"""
Telegram Bot API напрямую (aiohttp), как в торговом боте (там — requests).

Токен свой, не торгового бота: два опросчика одного токена ломают друг друга.
Отказ Telegram торговлю не останавливает — строка в журнал.
"""

import json

from copier import config
from copier.log import log, scrub


def _button(text, data):
    if str(data).startswith(('https://', 'http://')):
        return {'text': text, 'url': data}
    return {'text': text, 'callback_data': data}


def keyboard(rows):
    """[[('текст', 'данные' | 'https://…'), …], …] → inline_keyboard. Данные ≤ 64 байт."""
    return {'inline_keyboard': [[_button(text, data) for text, data in row] for row in rows if row]}


class TgApi:
    def __init__(self, token=None, send=None):
        self.token = token if token is not None else config.TG_TOKEN
        self._send_fn = send
        self._session = None

    @property
    def enabled(self):
        return bool(self.token)

    async def _post(self, method, payload, timeout=20):
        if self._send_fn is not None:
            return await self._send_fn(method, payload)
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = f'https://api.telegram.org/bot{self.token}/{method}'
        try:
            async with self._session.post(url, json=payload,
                                          timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                data = json.loads(await resp.text() or '{}')
                if not data.get('ok'):
                    log(f'telegram {method}: {scrub(data.get("description"))}')
                    return None
                return data.get('result')
        except Exception as exc:                       # noqa: BLE001
            log(f'telegram {method}: {scrub(exc)}')
            return None

    async def updates(self, offset, timeout=25):
        return await self._post('getUpdates', {'offset': offset, 'timeout': timeout,
                                               'allowed_updates': ['message', 'callback_query']},
                                timeout=timeout + 10) or []

    async def send(self, chat_id, text, kb=None):
        payload = {'chat_id': chat_id, 'text': text[:4000], 'disable_web_page_preview': True}
        if kb:
            payload['reply_markup'] = keyboard(kb)
        return await self._post('sendMessage', payload)

    async def edit(self, chat_id, message_id, text, kb=None):
        payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text[:4000],
                   'disable_web_page_preview': True}
        if kb:
            payload['reply_markup'] = keyboard(kb)
        return await self._post('editMessageText', payload)

    async def delete(self, chat_id, message_id):
        return await self._post('deleteMessage', {'chat_id': chat_id, 'message_id': message_id})

    async def answer(self, callback_id, text=None):
        payload = {'callback_query_id': callback_id}
        if text:
            payload['text'] = text[:190]
        return await self._post('answerCallbackQuery', payload)

    async def set_commands(self, commands):
        return await self._post('setMyCommands', {'commands': [
            {'command': c, 'description': d} for c, d in commands]})

    async def me(self):
        return await self._post('getMe', {})

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
