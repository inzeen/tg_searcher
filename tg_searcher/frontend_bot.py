# -*- coding: utf-8 -*-
import html
from time import time
from typing import Optional, List, Tuple, Set, Union
from traceback import format_exc
from argparse import ArgumentParser, ArgumentError # 导入 ArgumentError
import shlex

import redis
import whoosh.index # 导入 whoosh.index 以便捕获 LockError
from telethon import TelegramClient, events, Button
from telethon.tl.types import BotCommand, BotCommandScopePeer, BotCommandScopeDefault, MessageEntityMentionName
from telethon.tl.custom import Message as TgMessage
from telethon.tl.functions.bots import SetBotCommandsRequest
import telethon.errors.rpcerrorlist as rpcerrorlist
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from .common import CommonBotConfig, get_logger, get_share_id, remove_first_word, brief_content
from .backend_bot import BackendBot, EntityNotFoundError
from .indexer import SearchResult, IndexMsg # 确保 IndexMsg 已更新

# 获取日志记录器
logger = get_logger('frontend_bot')


class BotFrontendConfig:
    @staticmethod
    def _parse_redis_cfg(redis_cfg: str) -> Tuple[str, int]:
        colon_idx = redis_cfg.find(':') # 使用 find 避免 ValueError
        if colon_idx < 0:
            # 假设默认端口 6379
            return redis_cfg, 6379
        try:
            host = redis_cfg[:colon_idx] if colon_idx > 0 else redis_cfg # 处理只有端口的情况 (e.g., :6379)
            port = int(redis_cfg[colon_idx + 1:])
            return host or 'localhost', port # 如果 host 为空，默认为 localhost
        except (ValueError, TypeError):
            raise ValueError(f"Invalid Redis port number in '{redis_cfg}'")

    def __init__(self, **kw):
        try:
            self.bot_token: str = kw['bot_token']
            self.admin: Union[int, str] = kw['admin_id']
        except KeyError as e:
             raise ValueError(f"Missing required configuration key: {e}")

        self.page_len: int = kw.get('page_len', 10)
        if self.page_len <= 0:
             logger.warning("page_len must be positive, using default 10.")
             self.page_len = 10

        self.no_redis: bool = kw.get('no_redis', False)
        self.redis_host: Optional[Tuple[str, int]] = None # 初始设为 None

        if not self.no_redis:
             try:
                  redis_cfg = kw.get('redis', 'localhost:6379') # 默认值
                  if redis_cfg: # 确保配置值不为空
                    self.redis_host = self._parse_redis_cfg(redis_cfg)
                  else:
                     logger.warning(f"Redis config value is empty. Disabling redis.")
                     self.no_redis = True
             except ValueError as e:
                  logger.error(f"Error parsing redis config '{kw.get('redis')}': {e}. Disabling redis.")
                  self.no_redis = True
             except KeyError:
                  logger.info(f"Redis config key 'redis' not found. Disabling redis.")
                  self.no_redis = True


        self.private_mode: bool = kw.get('private_mode', False)
        # 白名单应该只存整数 ID
        self.private_whitelist: Set[int] = set()
        raw_whitelist = kw.get('private_whitelist', [])
        if raw_whitelist:
             if isinstance(raw_whitelist, list):
                  for item in raw_whitelist:
                      try:
                          self.private_whitelist.add(int(item))
                      except (ValueError, TypeError):
                           logger.warning(f"Could not parse whitelist item '{item}' as int.")
             else:
                  logger.warning("private_whitelist format is not a list, ignoring.")
        # admin 自动加入白名单的操作移到 start() 中


class FakeRedis:
    """
    用内存字典模拟 Redis 接口，用于无 Redis 的轻量部署。
    """
    def __init__(self):
        self._data = {}
        self._logger = get_logger('FakeRedis')
        self._logger.warning("Using FakeRedis: Data will not persist across restarts.")

    def get(self, key): return self._data.get(key)
    def set(self, key, val, ex=None):
        if ex: self._logger.debug(f"FakeRedis received set with ex={ex} for key {key}, expiration ignored.")
        self._data[key] = str(val)
    def delete(self, *keys):
        deleted_count = 0
        for key in keys:
             if key in self._data:
                  del self._data[key]; deleted_count += 1
        return deleted_count
    def ping(self): return True


class BotFrontend:
    """
    Redis 数据协议 (keys):
    - {frontend_id}:query_text:{bot_chat_id}:{result_msg_id} -> 搜索结果对应的查询文本
    - {frontend_id}:query_chats:{bot_chat_id}:{result_msg_id} -> 搜索结果对应的聊天筛选 (逗号分隔的 chat_id)
    - {frontend_id}:select_chat:{bot_chat_id}:{selection_msg_id} -> 用户通过按钮选择的 chat_id
    """

    def __init__(self, common_cfg: CommonBotConfig, cfg: BotFrontendConfig, frontend_id: str, backend: BackendBot):
        self.backend = backend
        self.id = frontend_id
        self._common_cfg = common_cfg
        self.bot = TelegramClient(
            str(common_cfg.session_dir / f'frontend_{self.id}.session'),
            api_id=common_cfg.api_id, api_hash=common_cfg.api_hash, proxy=common_cfg.proxy
        )
        self._cfg = cfg
        self._redis: Union[redis.client.Redis, FakeRedis]
        if cfg.no_redis or cfg.redis_host is None:
            self._redis = FakeRedis()
        else:
            try:
                 self._redis = Redis(host=cfg.redis_host[0], port=cfg.redis_host[1], decode_responses=True)
                 self._redis.ping()
            except RedisConnectionError as e:
                 logger.critical(f'Cannot connect to Redis server {cfg.redis_host}: {e}. Falling back to FakeRedis.')
                 self._redis = FakeRedis()
                 self._cfg.no_redis = True
            except Exception as e:
                 logger.critical(f'Error initializing Redis client {cfg.redis_host}: {e}. Falling back to FakeRedis.')
                 self._redis = FakeRedis()
                 self._cfg.no_redis = True

        self._logger = logger # 使用模块级 logger
        self._admin_id = None
        self.username = None
        self.my_id = None # Bot 自身 ID

        # 参数解析器
        # prog 置空，让 help 信息更简洁
        # exit_on_error=False 让我们能捕获错误并回复用户
        self.download_arg_parser = ArgumentParser(prog="", add_help=False, exit_on_error=False)
        self.download_arg_parser.add_argument('--min', type=int, default=0, help="Minimum message ID")
        self.download_arg_parser.add_argument('--max', type=int, default=0, help="Maximum message ID (0=no limit)")
        self.download_arg_parser.add_argument('chats', type=str, nargs='*', help="Chat IDs or usernames")

        self.chat_ids_parser = ArgumentParser(prog="", add_help=False, exit_on_error=False)
        self.chat_ids_parser.add_argument('chats', type=str, nargs='*', help="Chat IDs or usernames")

    async def start(self):
        # 解析管理员 ID
        try:
            if not self._cfg.admin: raise ValueError("Admin ID is not configured.")
            self._admin_id = await self.backend.str_to_chat_id(str(self._cfg.admin))
            self._logger.info(f"Admin ID resolved to: {self._admin_id}")
            if self._cfg.private_mode and self._admin_id:
                 self._cfg.private_whitelist.add(self._admin_id)
                 self._logger.info(f"Admin {self._admin_id} added to private whitelist.")
        except EntityNotFoundError:
             self._logger.critical(f"Admin entity '{self._cfg.admin}' not found by backend session.")
             self._admin_id = None
             self._logger.error("Proceeding without a valid admin ID.")
        except (ValueError, TypeError) as e:
             self._logger.critical(f"Invalid admin configuration '{self._cfg.admin}': {e}")
             self._admin_id = None
             self._logger.error("Proceeding without a valid admin ID.")
        except Exception as e:
             self._logger.critical(f"Error resolving admin entity '{self._cfg.admin}': {e}", exc_info=True)
             self._admin_id = None
             self._logger.error("Proceeding without a valid admin ID.")

        # 检查 Redis 连接
        if not isinstance(self._redis, FakeRedis):
             try: self._redis.ping(); self._logger.info(f"Successfully connected to Redis at {self._cfg.redis_host}")
             except RedisConnectionError as e:
                  self._logger.critical(f'Redis connection failed after init: {e}. Falling back to FakeRedis.'); self._redis = FakeRedis(); self._cfg.no_redis = True

        self._logger.info(f'Starting frontend bot {self.id}...')
        try:
             await self.bot.start(bot_token=self._cfg.bot_token)
             me = await self.bot.get_me()
             if me is None: raise RuntimeError("Failed to get bot info (get_me() returned None)")
             self.username = me.username
             self.my_id = me.id # 保存自身 ID
             self._logger.info(f'Bot (@{self.username}, id={self.my_id}) account login ok')

             # 加入后端排除列表
             self.backend.excluded_chats.add(get_share_id(self.my_id))
             self._logger.info(f"Added bot ID {self.my_id} to backend's excluded chats.")

             await self._register_commands()
             self._logger.info(f'Register bot commands ok')
             self._register_hooks()

             if self._admin_id:
                  try:
                       msg_head = '✅ Bot 前端初始化完成\n\n'
                       stat_text = await self.backend.get_index_status(length_limit=4000 - len(msg_head))
                       await self.bot.send_message(self._admin_id, msg_head + stat_text, parse_mode='html', link_preview=False)
                  except Exception as e:
                       error_msg = f'⚠️ Bot 启动，但获取初始状态失败: {e}'
                       self._logger.error(f"Failed to get initial status: {e}", exc_info=True)
                       try: await self.bot.send_message(self._admin_id, error_msg)
                       except Exception as final_e: self._logger.error(f"Failed to send startup status/error message to admin {self._admin_id}: {final_e}")
             else: self._logger.warning("Admin ID not configured/invalid, skipping startup message.")
             self._logger.info(f"Frontend bot {self.id} started successfully.")
        except Exception as e:
             self._logger.critical(f"Failed to start frontend bot {self.id}: {e}", exc_info=True)
             # exit(1) # 考虑是否退出


    async def _callback_handler(self, event: events.CallbackQuery.Event):
        try:
            self._logger.info(f'Callback query ({event.message_id}) from {event.sender_id} in chat {event.chat_id}, data={event.data!r}')
            if not event.data: await event.answer("无效操作 (no data)。"); return
            try: query_data = event.data.decode('utf-8')
            except (UnicodeDecodeError, AttributeError): await event.answer("无效操作 (bad data format)。"); return
            if not query_data.strip(): await event.answer("无效操作 (empty data)。"); return

            parts = query_data.split('=', 1)
            if len(parts) != 2: self._logger.warning(f"Invalid callback data format: {query_data}"); await event.answer("操作格式错误。"); return

            action, value = parts[0], parts[1]
            redis_prefix = f'{self.id}:'
            bot_chat_id = event.chat_id
            result_msg_id = event.message_id

            if action == 'search_page':
                 try: page_num = int(value); assert page_num > 0
                 except (ValueError, AssertionError): await event.answer("无效页码。"); return

                 query_key = f'{redis_prefix}query_text:{bot_chat_id}:{result_msg_id}'
                 chats_key = f'{redis_prefix}query_chats:{bot_chat_id}:{result_msg_id}'
                 q, chats_str = self._redis.get(query_key), self._redis.get(chats_key)

                 if q is None:
                     try: await event.edit("抱歉，无法找到此搜索的原始查询信息（可能已过期），请重新搜索。")
                     except rpcerrorlist.MessageNotModifiedError: pass
                     except Exception as edit_e: self._logger.error(f"Failed to edit message for expired query error: {edit_e}")
                     if chats_str is not None: self._redis.delete(chats_key)
                     await event.answer("搜索信息已过期。"); return

                 chats = None
                 if chats_str:
                     try: chats = [int(cid) for cid in chats_str.split(',') if cid.strip()]
                     except ValueError: self._logger.warning(f"Invalid chat IDs in Redis: {chats_str}"); chats = None

                 self._logger.info(f'Query [{brief_content(q)}] (chats={chats}) turned to page {page_num}')
                 start_time = time()
                 try: result = self.backend.search(q, chats, self._cfg.page_len, page_num)
                 except Exception as e: self._logger.error(f"Backend search failed: {e}", exc_info=True); await event.answer("搜索后端出错。"); return
                 used_time = time() - start_time

                 response = await self._render_response_text(result, used_time)
                 buttons = self._render_respond_buttons(result, page_num)
                 try: await event.edit(response, parse_mode='html', buttons=buttons, link_preview=False); await event.answer()
                 except rpcerrorlist.MessageNotModifiedError: self._logger.info("Message not modified."); await event.answer()
                 except rpcerrorlist.MessageIdInvalidError: self._logger.warning(f"Message {result_msg_id} deleted."); await event.answer("消息已被删除。")
                 except Exception as e: self._logger.error(f"Failed to edit message: {e}"); await event.answer("更新搜索结果失败。")

            elif action == 'select_chat':
                 try:
                      chat_id = int(value)
                      try:
                          chat_name = await self.backend.translate_chat_id(chat_id)
                          reply_prompt = f'☑️ 已选择对话: **{html.escape(chat_name)}** (`{chat_id}`)\n\n回复本条消息可对此对话执行操作。'
                      except EntityNotFoundError:
                          reply_prompt = f'☑️ 已选择对话: `{chat_id}` (无法获取名称)\n\n回复本条消息可对此对话执行操作。'
                      await event.edit(reply_prompt, parse_mode='markdown')
                      select_key = f'{redis_prefix}select_chat:{bot_chat_id}:{result_msg_id}'
                      self._redis.set(select_key, chat_id, ex=3600)
                      self._logger.info(f"Chat {chat_id} selected by {event.sender_id}, key {select_key}")
                      await event.answer("对话已选择")
                 except ValueError: await event.answer("无效的对话 ID。")
                 except Exception as e: self._logger.error(f"Error processing select_chat: {e}", exc_info=True); await event.answer("处理选择对话时出错。")

            elif action == 'noop': await event.answer()
            else: self._logger.warning(f'Unknown callback action: {action}'); await event.answer("未知操作。")
        except Exception as e:
             self._logger.error(f"Exception in callback handler: {e}", exc_info=True)
             try: await event.answer("处理请求时发生内部错误。")
             except Exception as final_e: self._logger.error(f"Failed to answer callback after error: {final_e}")


    async def _normal_msg_handler(self, event: events.NewMessage.Event):
        text: str = event.raw_text.strip()
        sender_entity = await event.message.get_sender()
        sender_id = sender_entity.id if sender_entity else 'Unknown'
        self._logger.info(f'User {sender_id} (in chat {event.chat_id}) sends: "{brief_content(text, 100)}"')

        selected_chat_context = await self._get_selected_chat_from_reply(event)

        if not text or text.startswith('/start'):
            await event.reply("欢迎使用 TG Searcher Bot！\n发送关键词进行搜索，或使用 /help 查看可用命令。")
            return

        elif text.startswith('/help'):
             help_text = """
**可用命令:**
/search `关键词` - 搜索消息 (直接发送关键词也可)。
/chats `[关键词]` - 列出并选择已索引的对话。
/random - 返回一条随机消息。
/help - 显示此帮助信息。

**管理员命令:**
/download_chat `[选项] [对话...]` - 下载并索引对话历史。
/monitor_chat `对话...` - 将对话加入实时监听。
/clear `[对话...|all]` - 清除索引。
/stat - 查看后端状态。
/find_chat_id `关键词` - 根据名称查找对话 ID。
/refresh_chat_names - 刷新后端对话名称缓存。

**使用 /chats 选择对话后:**
- 回复选择成功的消息 + 搜索词，可仅搜索该对话。
- 回复选择成功的消息 + 管理命令 (如 /download_chat)，可对该对话执行操作。
             """
             await event.reply(help_text, parse_mode='markdown')

        elif text.startswith('/random'):
            try:
                msg = self.backend.rand_msg()
                chat_name = await self.backend.translate_chat_id(msg.chat_id)
                display_content = ""
                if msg.filename: display_content += f"📎 {html.escape(msg.filename)}"
                if msg.content:
                     content_brief = html.escape(brief_content(msg.content))
                     if msg.filename: display_content += f" ({content_brief})"
                     else: display_content = content_brief
                if not display_content: display_content = "(空消息)" # 以防万一

                respond = f'随机消息来自 **{html.escape(chat_name)}** (`{msg.chat_id}`)\n'
                if msg.sender: respond += f'发送者: {html.escape(msg.sender)}\n'
                respond += f'时间: {msg.post_time.strftime("%Y-%m-%d %H:%M")}\n'
                respond += f'内容: {display_content}\n'
                respond += f'<a href="{msg.url}">跳转到消息</a>'
            except IndexError: respond = '错误：索引为空，无法获取随机消息。'
            except EntityNotFoundError as e: respond = f"获取随机消息时出错: 无法找到来源对话 (`{e.entity}`)。"
            except Exception as e: self._logger.error(f"Error getting random message: {e}", exc_info=True); respond = f"获取随机消息时出错: {type(e).__name__}"
            await event.reply(respond, parse_mode='html', link_preview=False)

        elif text.startswith('/chats'):
            buttons = []
            kw = remove_first_word(text)
            monitored_chats_list = sorted(list(self.backend.monitored_chats))
            chats_found = 0
            if monitored_chats_list:
                for chat_id in monitored_chats_list:
                    try:
                         chat_name = await self.backend.translate_chat_id(chat_id)
                         if kw and kw.lower() not in chat_name.lower(): continue
                         chats_found += 1
                         if chats_found <= 50:
                              display_name = f"{brief_content(chat_name, 25)} (`{chat_id}`)"
                              buttons.append(Button.inline(display_name, f'select_chat={chat_id}'))
                    except EntityNotFoundError: self._logger.warning(f"Chat ID {chat_id} not found during /chats.")
                    except Exception as e: self._logger.error(f"Error processing chat {chat_id} for /chats: {e}")

                if buttons:
                     reply_text = "请选择一个对话进行操作：" if chats_found <= 50 else f"找到 {chats_found} 个对话，显示前 50 个："
                     button_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
                     await event.reply(reply_text, buttons=button_rows)
                else: await event.reply(f'没有找到标题包含 "{html.escape(kw)}" 的已索引对话。' if kw else '没有已索引的对话。')
            else: await event.reply('暂无监听或索引的对话，请管理员使用 /download_chat 或 /monitor_chat 添加。')

        elif text.startswith('/search'):
            query = remove_first_word(text)
            if not query and not selected_chat_context: await event.reply("请输入要搜索的关键词。用法: `/search 关键词`", parse_mode='markdown'); return
            await self._search(event, query, selected_chat_context)

        elif text.startswith('/'):
            command = text.split()[0]
            await event.reply(f'错误：未知命令 `{command}`。请使用 /help 查看可用命令。', parse_mode='markdown')

        else:
            await self._search(event, text, selected_chat_context)


    async def _chat_ids_from_args(self, chats_args: List[str]) -> Tuple[List[int], List[str]]:
        chat_ids, errors = [], []
        if not chats_args: return [], []
        for chat_arg in chats_args:
            try: chat_ids.append(await self.backend.str_to_chat_id(chat_arg))
            except EntityNotFoundError: errors.append(f'未找到对话 "{html.escape(chat_arg)}"')
            except Exception as e: errors.append(f'解析对话 "{html.escape(chat_arg)}" 时出错: {type(e).__name__}')
        return chat_ids, errors


    async def _admin_msg_handler(self, event: events.NewMessage.Event):
        text: str = event.raw_text.strip()
        self._logger.info(f'Admin {event.chat_id} sends command: "{brief_content(text, 100)}"')
        selected_chat_context = await self._get_selected_chat_from_reply(event)
        selected_chat_id = selected_chat_context[0] if selected_chat_context else None
        selected_chat_name = selected_chat_context[1] if selected_chat_context else None

        # --- 所有命令处理块统一用 if/elif/else ---
        if text.startswith('/stat'):
            try: await event.reply(await self.backend.get_index_status(), parse_mode='html', link_preview=False)
            except Exception as e: self._logger.error("Error getting backend status:", exc_info=True); error_trace = html.escape(format_exc()); await event.reply(f"获取后端状态时出错: {html.escape(str(e))}\n<pre>{error_trace}</pre>", parse_mode='html')

        elif text.startswith('/download_chat'):
            try: args = self.download_arg_parser.parse_args(shlex.split(text)[1:])
            except (ArgumentError, Exception) as e: usage = self.download_arg_parser.format_help(); await event.reply(f"参数解析错误: {e}\n用法:\n<pre>{html.escape(usage)}</pre>", parse_mode='html'); return
            min_id, max_id = args.min or 0, args.max or 0
            target_chat_ids, errors = await self._chat_ids_from_args(args.chats)
            if not args.chats and selected_chat_id is not None and selected_chat_id not in target_chat_ids: target_chat_ids = [selected_chat_id]; await event.reply(f"检测到回复，将对选择的对话 **{html.escape(selected_chat_name)}** (`{selected_chat_id}`) 执行下载。", parse_mode='markdown')
            elif not target_chat_ids and not errors: await event.reply("错误：请指定对话或回复选择对话的消息。"); return
            if errors: await event.reply("以下对话无法解析:\n- " + "\n- ".join(errors));
            if not target_chat_ids: return
            # 执行下载... (错误处理已移入循环)
            success_count, fail_count = 0, 0
            for chat_id in target_chat_ids:
                self._logger.info(f'Admin triggered download history for {chat_id} (min={min_id}, max={max_id})')
                try: await self._download_history(event, chat_id, min_id, max_id); success_count += 1
                except Exception as dl_e:
                    fail_count += 1; self._logger.error(f"Download failed for chat {chat_id}: {dl_e}", exc_info=True)
                    try: chat_html = await self.backend.format_dialog_html(chat_id); await event.reply(f"❌ 下载 {chat_html} 失败: {html.escape(str(dl_e))}", parse_mode='html')
                    except Exception: await event.reply(f"❌ 下载对话 `{chat_id}` 失败: {html.escape(str(dl_e))}", parse_mode='html')
            if len(target_chat_ids) > 1: await event.reply(f"下载任务完成：{success_count} 成功, {fail_count} 失败。")

        elif text.startswith('/monitor_chat'):
            try: args = self.chat_ids_parser.parse_args(shlex.split(text)[1:])
            except (ArgumentError, Exception) as e: usage = self.chat_ids_parser.format_help(); await event.reply(f"参数解析错误: {e}\n用法:\n<pre>{html.escape(usage)}</pre>", parse_mode='html'); return
            target_chat_ids, errors = await self._chat_ids_from_args(args.chats)
            if not args.chats and selected_chat_id is not None and selected_chat_id not in target_chat_ids: target_chat_ids = [selected_chat_id]; await event.reply(f"检测到回复，将对选择的对话 **{html.escape(selected_chat_name)}** (`{selected_chat_id}`) 加入监听。", parse_mode='markdown')
            elif not target_chat_ids and not errors: await event.reply("错误：请指定对话或回复选择对话的消息。"); return
            if errors: await event.reply("以下对话无法解析:\n- " + "\n- ".join(errors));
            if not target_chat_ids: return
            # 执行监听...
            replies, added_count, already_monitored = [], 0, 0
            for chat_id in target_chat_ids:
                if chat_id in self.backend.monitored_chats: already_monitored += 1
                else:
                    self.backend.monitored_chats.add(chat_id); added_count += 1
                    try: chat_html = await self.backend.format_dialog_html(chat_id); replies.append(f"- ✅ {chat_html} 已成功加入监听。")
                    except Exception as e: replies.append(f"- ✅ 对话 `{chat_id}` 已加入监听 (无法获取名称: {type(e).__name__})。")
                    self._logger.info(f'Admin added {chat_id} to monitored_chats')
            if replies: await event.reply('\n'.join(replies), parse_mode='html', link_preview=False)
            summary = [f"{c} 个对话已加入监听。" for c in [added_count] if c > 0] + [f"{c} 个对话已在监听中。" for c in [already_monitored] if c > 0]
            if summary: await event.reply(" ".join(summary))

        elif text.startswith('/clear'):
            try: args = self.chat_ids_parser.parse_args(shlex.split(text)[1:])
            except (ArgumentError, Exception) as e: usage = self.chat_ids_parser.format_help(); await event.reply(f"参数解析错误: {e}\n用法:\n<pre>{html.escape(usage)}</pre>", parse_mode='html'); return
            if len(args.chats) == 1 and args.chats[0].lower() == 'all':
                try: self.backend.clear(chat_ids=None); await event.reply('✅ 全部索引已成功清除。')
                except Exception as e: self._logger.error("Error during clear all:", exc_info=True); await event.reply(f"清除全部索引时出错: {e}")
                return
            target_chat_ids, errors = await self._chat_ids_from_args(args.chats)
            if not args.chats and selected_chat_id is not None and selected_chat_id not in target_chat_ids: target_chat_ids = [selected_chat_id]; await event.reply(f"检测到回复，将清除选择的对话 **{html.escape(selected_chat_name)}** (`{selected_chat_id}`) 的索引。", parse_mode='markdown')
            elif not target_chat_ids and not errors: await event.reply("错误：请指定对话或回复选择对话的消息，或使用 /clear all。"); return
            if errors: await event.reply("以下对话无法解析:\n- " + "\n- ".join(errors));
            if not target_chat_ids: return
            # 执行清除...
            share_ids_to_clear = target_chat_ids
            self._logger.info(f'Admin triggered clear index for chats: {share_ids_to_clear}')
            try:
                self.backend.clear(chat_ids=share_ids_to_clear)
                replies = []
                for chat_id in share_ids_to_clear:
                    try: chat_html = await self.backend.format_dialog_html(chat_id); replies.append(f"- ✅ {chat_html} 的索引已清除。")
                    except Exception: replies.append(f"- ✅ 对话 `{chat_id}` 的索引已清除 (无法获取名称)。")
                await event.reply('\n'.join(replies), parse_mode='html', link_preview=False)
            except Exception as e: self._logger.error(f"Error clearing index: {e}", exc_info=True); await event.reply(f"清除索引时出错: {e}")

        elif text.startswith('/refresh_chat_names'):
            msg = await event.reply('正在刷新后端的对话名称缓存...')
            try: await self.backend.session.refresh_translate_table(); await msg.edit('✅ 对话名称缓存刷新完成。')
            except Exception as e: self._logger.error("Error refreshing chat names:", exc_info=True); await msg.edit(f'刷新对话名称缓存时出错: {e}')

        elif text.startswith('/find_chat_id'):
            q = remove_first_word(text)
            if not q: await event.reply('错误：请输入关键词。'); return
            try:
                chat_results = await self.backend.find_chat_id(q)
                sb = []
                if chat_results:
                     sb.append(f'找到 {len(chat_results)} 个标题中包含 "{html.escape(q)}" 的对话:\n')
                     for chat_id in chat_results[:50]:
                         try: chat_name = await self.backend.translate_chat_id(chat_id); sb.append(f'- {html.escape(chat_name)}: `{chat_id}`\n')
                         except EntityNotFoundError: sb.append(f'- 未知对话: `{chat_id}`\n')
                         except Exception as e: sb.append(f'- 对话 `{chat_id}` 获取名称失败: {type(e).__name__}\n')
                     if len(chat_results) > 50: sb.append("\n(仅显示前 50 个结果)")
                else: sb.append(f'未找到标题中包含 "{html.escape(q)}" 的对话。')
                await event.reply(''.join(sb), parse_mode='html')
            except Exception as e: self._logger.error(f"Error finding chat ID: {e}", exc_info=True); await event.reply(f"查找对话 ID 时出错: {e}")

        else:
            # 管理员发送未知命令或普通文本，也按普通消息处理
            await self._normal_msg_handler(event)


    async def _search(self, event: events.NewMessage.Event, query: str, selected_chat_context: Optional[Tuple[int, str]]):
        """执行搜索"""
        if not query and selected_chat_context:
             query = '*'
             await event.reply(f"将搜索对话 **{html.escape(selected_chat_context[1])}** (`{selected_chat_context[0]}`) 中的所有已索引消息。", parse_mode='markdown')
        elif not query:
             self._logger.debug("Empty search query received, ignoring.")
             return

        target_chat_id_list = [selected_chat_context[0]] if selected_chat_context else None
        try:
            is_target_empty = self.backend.is_empty(chat_id=target_chat_id_list[0] if target_chat_id_list else None)
        except Exception as e:
             self._logger.error(f"Error checking index emptiness: {e}"); await event.reply("检查索引状态时出错。"); return

        if is_target_empty:
             if selected_chat_context: await event.reply(f'对话 **{html.escape(selected_chat_context[1])}** (`{selected_chat_context[0]}`) 索引为空。', parse_mode='markdown')
             else: await event.reply('当前全局索引为空。')
             return

        start_time = time()
        search_context_info = f"in chat {selected_chat_context[0]}" if selected_chat_context else "globally"
        self._logger.info(f'Searching "{brief_content(query, 100)}" {search_context_info}')

        try:
            result = self.backend.search(query, in_chats=target_chat_id_list, page_len=self._cfg.page_len, page_num=1)
            used_time = time() - start_time

            respond_text = await self._render_response_text(result, used_time)
            buttons = self._render_respond_buttons(result, 1)

            msg: Optional[TgMessage] = await event.reply(respond_text, parse_mode='html', buttons=buttons, link_preview=False)

            if msg:
                redis_prefix = f'{self.id}:'
                bot_chat_id = event.chat_id
                result_msg_id = msg.id
                query_key = f'{redis_prefix}query_text:{bot_chat_id}:{result_msg_id}'
                chats_key = f'{redis_prefix}query_chats:{bot_chat_id}:{result_msg_id}'
                self._redis.set(query_key, query, ex=3600) # 存储原始 query
                if target_chat_id_list:
                     chats_str = ','.join(map(str, target_chat_id_list)) if target_chat_id_list else ''
                     if chats_str: self._redis.set(chats_key, chats_str, ex=3600)
                     else: self._redis.delete(chats_key)
                else: self._redis.delete(chats_key)
            else: self._logger.error("Failed to send search result message.")
        except whoosh.index.LockError: await event.reply('⏳ 索引当前正在写入中，请稍后再试。')
        except Exception as e: self._logger.error(f"Error during search: {e}", exc_info=True); await event.reply(f'搜索时发生错误: {type(e).__name__}。')


    async def _download_history(self, event: events.NewMessage.Event, chat_id: int, min_id: int, max_id: int):
         # chat_id 已经是 share_id
         try: chat_html = await self.backend.format_dialog_html(chat_id)
         except Exception as e: self._logger.error(f"Failed to format chat html for {chat_id}: {e}"); chat_html = f"对话 `{chat_id}`"

         # 检查索引是否为空的警告
         try:
             if min_id == 0 and max_id == 0 and not self.backend.is_empty(chat_id=chat_id):
                 await event.reply(f'⚠️ 警告: {chat_html} 的索引已存在。\n重新下载全部历史可能导致重复。请先 `/clear {chat_id}` 或指定范围。', parse_mode='html')
         except Exception as e: self._logger.error(f"Error checking index emptiness for {chat_id}: {e}")

         prog_msg: Optional[TgMessage] = None
         last_update_time = time()
         update_interval = 5
         total_downloaded_count = 0

         async def call_back(current_msg_id: int, downloaded_count: int):
             nonlocal prog_msg, last_update_time, total_downloaded_count
             total_downloaded_count = downloaded_count
             now = time()
             if now - last_update_time > update_interval:
                 last_update_time = now
                 prog_text = f'⏳ 正在下载 {chat_html}:\n已处理 {downloaded_count} 条，当前 ID: {current_msg_id}'
                 try:
                     if prog_msg is None: prog_msg = await event.reply(prog_text, parse_mode='html')
                     else: await prog_msg.edit(prog_text, parse_mode='html')
                 except rpcerrorlist.FloodWaitError as fwe: self._logger.warning(f"Flood wait ({fwe.seconds}s) updating download progress."); last_update_time += fwe.seconds
                 except rpcerrorlist.MessageNotModifiedError: pass
                 except rpcerrorlist.MessageIdInvalidError: self._logger.warning(f"Progress message deleted."); prog_msg = None
                 except Exception as e: self._logger.error(f"Failed to edit progress message: {e}"); prog_msg = None

         start_time = time()
         try:
              # download_history 内部处理了 EntityNotFoundError 等
              await self.backend.download_history(chat_id, min_id, max_id, call_back)
              used_time = time() - start_time
              completion_msg = f'✅ {chat_html} 下载完成，共索引 {total_downloaded_count} 条消息，用时 {used_time:.2f} 秒。'
              try: await event.reply(completion_msg, parse_mode='html')
              except Exception: await self.bot.send_message(event.chat_id, completion_msg, parse_mode='html')
         # EntityNotFoundError 由 download_history 内部处理并抛出，这里捕获并回复
         except EntityNotFoundError as e:
              self._logger.error(f"Download failed for {chat_id}: {e}")
              await event.reply(f'❌ 下载 {chat_html} 时出错: {e}', parse_mode='html')
              # 无需再 discard，内部已处理
         except ValueError as e: # 捕获 chat_id 排除的错误
              self._logger.error(f"Download failed for {chat_id}: {e}")
              await event.reply(f'❌ 下载 {chat_html} 时出错: {e}', parse_mode='html')
         except Exception as e: # 其他未知错误
              self._logger.error(f"Download failed for {chat_id}:", exc_info=True)
              await event.reply(f'❌ 下载 {chat_html} 时发生未知错误: {type(e).__name__}', parse_mode='html')
         finally:
              if prog_msg:
                   try: await prog_msg.delete()
                   except Exception as e: self._logger.warning(f"Failed to delete progress message: {e}")


    def _register_hooks(self):
        @self.bot.on(events.CallbackQuery())
        async def callback_query_handler(event: events.CallbackQuery.Event):
             sender_id = event.sender_id
             is_whitelisted = sender_id in self._cfg.private_whitelist
             # 管理员总是允许
             if self._cfg.private_mode and not is_whitelisted and sender_id != self._admin_id:
                   self._logger.warning(f"Blocked callback query from non-whitelisted user {sender_id}.")
                   await event.answer("抱歉，您无权使用此按钮。", alert=True); return
             await self._callback_handler(event)

        @self.bot.on(events.NewMessage())
        async def bot_message_handler(event: events.NewMessage.Event):
            sender = await event.message.get_sender()
            if not sender: self._logger.debug("Ignoring message with no sender info."); return
            sender_id = sender.id
            if not self.my_id: # 如果 bot ID 未初始化
                 try: self.my_id = (await self.bot.get_me()).id
                 except Exception: self._logger.error("Failed to get self ID in handler."); return # 获取失败则无法处理

            if sender_id == self.my_id: return

            is_admin = (self._admin_id is not None and sender_id == self._admin_id)

            is_mentioned, is_reply_to_bot = False, False
            if event.is_group or event.is_channel:
                 if self.username and f'@{self.username}' in event.raw_text: is_mentioned = True
                 elif event.message.mentioned and event.message.entities:
                      for entity in event.message.entities:
                          if isinstance(entity, MessageEntityMentionName) and entity.user_id == self.my_id: is_mentioned = True; break
                 if event.message.is_reply:
                      try: # 获取回复消息可能失败
                          reply_msg = await event.message.get_reply_message()
                          if reply_msg and reply_msg.sender_id == self.my_id: is_reply_to_bot = True
                      except Exception as e: self._logger.warning(f"Failed to get reply message: {e}")

            should_process = event.is_private or is_mentioned or is_reply_to_bot
            if not should_process: self._logger.debug(f"Ignoring msg in group {event.chat_id} from {sender_id}."); return

            if self._cfg.private_mode and not is_admin:
                 try: chat_share_id = get_share_id(event.chat_id)
                 except Exception: chat_share_id = None
                 is_sender_whitelisted = sender_id in self._cfg.private_whitelist
                 is_chat_whitelisted = chat_share_id is not None and chat_share_id in self._cfg.private_whitelist
                 if not is_sender_whitelisted and not is_chat_whitelisted:
                     self._logger.info(f"Blocked access user {sender_id} chat {event.chat_id}({chat_share_id}) private mode.")
                     if event.is_private: await event.reply('抱歉，由于隐私设置，您无法使用本机器人。')
                     return

            handler_task = self._admin_msg_handler if is_admin else self._normal_msg_handler
            if handler_task:
                 try: await handler_task(event)
                 except whoosh.index.LockError: await event.reply('⏳ 索引锁定中，请稍后。')
                 except EntityNotFoundError as e: await event.reply(f'❌ 未找到对话或用户: {e.entity}')
                 except telethon.errors.rpcerrorlist.UserIsBlockedError: self._logger.warning(f"User {sender_id} blocked bot.")
                 except telethon.errors.rpcerrorlist.ChatWriteForbiddenError: self._logger.warning(f"Write forbidden in chat {event.chat_id}.")
                 except Exception as e:
                      err_type = type(e).__name__; self._logger.error(f"Error handling msg from {sender_id}: {err_type}: {e}", exc_info=True)
                      try: await event.reply(f'处理请求出错: {err_type}。\n请联系管理员。')
                      except Exception as reply_e: self._logger.error(f"Failed to reply error msg: {reply_e}")
                      if self._admin_id and event.chat_id != self._admin_id:
                           try: await self.bot.send_message(self._admin_id, f"用户 {sender_id} (聊天 {event.chat_id}) 消息处理错误:\n<pre>{html.escape(format_exc())}</pre>", parse_mode='html')
                           except Exception as admin_notify_e: self._logger.error(f"Failed to notify admin: {admin_notify_e}")


    async def _get_selected_chat_from_reply(self, event: events.NewMessage.Event) -> Optional[Tuple[int, str]]:
        msg = event.message
        if not msg.is_reply: return None
        reply_to_msg_id = msg.reply_to_msg_id
        if not reply_to_msg_id: return None

        redis_prefix = f'{self.id}:'
        select_key = f'{redis_prefix}select_chat:{event.chat_id}:{reply_to_msg_id}'
        redis_result = self._redis.get(select_key)

        if redis_result:
            try:
                chat_id = int(redis_result) # share_id
                try: chat_name = await self.backend.translate_chat_id(chat_id)
                except EntityNotFoundError: chat_name = f"未知对话 ({chat_id})"
                self._logger.info(f"Msg from {event.sender_id} is reply to chat selection msg for chat {chat_id}")
                return chat_id, chat_name
            except ValueError: self._logger.warning(f"Invalid chat_id in Redis key {select_key}: {redis_result}"); self._redis.delete(select_key); return None
            except Exception as e: self._logger.error(f"Error processing selected chat context key {select_key}: {e}"); return None
        else: return None


    async def _register_commands(self):
        admin_input_peer = None
        if not self._admin_id: self._logger.warning("Admin ID invalid, skip admin commands.")
        else:
             try: admin_input_peer = await self.bot.get_input_entity(self._admin_id)
             except Exception as e: self._logger.error(f'Failed to get input entity for admin {self._admin_id}: {e}', exc_info=True)

        admin_cmds = [ BotCommand(c, d) for c, d in [
            ("download_chat", '[选项] [对话...] 下载索引'), ("monitor_chat", '对话... 加入监听'),
            ("clear", '[对话...|all] 清除索引'), ("stat", '查询状态'),
            ("find_chat_id", '关键词 查找对话ID'), ("refresh_chat_names", '刷新对话名称缓存')]]
        common_cmds = [ BotCommand(c, d) for c, d in [
            ("search", '关键词 搜索 (或直接发送)'), ("chats", '[关键词] 列出/选择对话'),
            ("random", '随机消息'), ("help", '显示帮助')]]

        if admin_input_peer:
            try: await self.bot(SetBotCommandsRequest(scope=BotCommandScopePeer(admin_input_peer), lang_code='', commands=admin_cmds + common_cmds)); self._logger.info(f"Set commands for admin {self._admin_id}.")
            except Exception as e: self._logger.error(f"Failed to set commands for admin: {e}", exc_info=True)
        try: await self.bot(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code='', commands=common_cmds)); self._logger.info("Set default commands.")
        except Exception as e: self._logger.error(f"Failed to set default commands: {e}", exc_info=True)


    async def _render_response_text(self, result: SearchResult, used_time: float) -> str:
        if not isinstance(result, SearchResult) or result.total_results == 0: return "未找到相关消息。"

        sb = [f'找到 {result.total_results} 条结果，用时 {used_time:.3f} 秒:\n\n']
        for i, hit in enumerate(result.hits, 1):
            try:
                msg: IndexMsg = hit.msg
                chat_title = await self.backend.translate_chat_id(msg.chat_id)
                header = [f"<b>{i}. {html.escape(chat_title)}</b>"]
                if msg.sender: header.append(f"(<u>{html.escape(msg.sender)}</u>)")
                header.append(f'[{msg.post_time.strftime("%y-%m-%d %H:%M")}]')
                sb.append(' '.join(header) + '\n')
                if msg.filename: sb.append(f"📎 文件: <b>{html.escape(msg.filename)}</b>\n")

                link_text = hit.highlighted.strip() if hit.highlighted else ""
                if not link_text:
                    if msg.content: link_text = html.escape(brief_content(msg.content, 50))
                    elif msg.filename: link_text = f"文件: {html.escape(brief_content(msg.filename, 50))}"
                    else: link_text = "跳转"

                if msg.url: sb.append(f'<a href="{html.escape(msg.url)}">{link_text}</a>\n\n')
                else: sb.append(f"{link_text} (无链接)\n\n")
            except EntityNotFoundError as e:
                 sb.append(f"<b>{i}.</b> 错误：无法找到对话 {e.entity}\n\n")
                 self._logger.warning(f"Entity not found for chat_id {msg.chat_id} in search result.")
            except Exception as e:
                 sb.append(f"<b>{i}.</b> 渲染结果时出错: {type(e).__name__}\n\n")
                 self._logger.error(f"Error rendering search hit: {e}", exc_info=True)

        final_text = ''.join(sb)
        max_len = 4096
        if len(final_text) > max_len:
             last_newline = final_text.rfind('\n\n', 0, max_len - 50)
             cutoff_msg = "\n\n...(结果过多，仅显示部分)"
             final_text = final_text[:last_newline if last_newline != -1 else max_len - len(cutoff_msg)] + cutoff_msg
        return final_text


    def _render_respond_buttons(self, result: SearchResult, cur_page_num: int) -> Optional[List[List[Button]]]:
        if not isinstance(result, SearchResult) or result.total_results == 0: return None
        try:
             page_len = self._cfg.page_len if self._cfg.page_len > 0 else 10
             total_pages = (result.total_results + page_len - 1) // page_len
        except ZeroDivisionError: total_pages = 1
        if total_pages <= 1: return None

        row = []
        if cur_page_num > 1: row.append(Button.inline('⬅️ 上一页', f'search_page={cur_page_num - 1}'))
        row.append(Button.inline(f'{cur_page_num}/{total_pages}', 'noop'))
        # 使用 is_last_page 判断更准确
        if not result.is_last_page: row.append(Button.inline('下一页 ➡️', f'search_page={cur_page_num + 1}'))

        return [row] if row else None
