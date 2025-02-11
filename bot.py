

import logging
import os
import asyncio
import time
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

from telethon import TelegramClient, errors
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError, PeerIdInvalidError
)

########################################
# Logging
########################################
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

########################################
# Глобальные переменные
########################################
BOT_TOKEN =  "7564269477:AAERD3vSP2gA6zYhBkoydZbEfX9lJKxozTk"   # <-- Вставьте токен бота
USER_STATE = {}        # user_id -> state
USER_TAGGER_TASKS = {} # user_id -> asyncio.Task

# Возможные состояния:
#  - "MAIN_MENU"
#  - "CHOOSE_ACCOUNT"
#  - "ENTER_API_ID_1", "ENTER_API_HASH_1", "ENTER_PHONE_1", "WAITING_CODE_1", "WAITING_PASSWORD_1"
#  - "ENTER_API_ID_2", "ENTER_API_HASH_2", "ENTER_PHONE_2", "WAITING_CODE_2", "WAITING_PASSWORD_2"
#  - "WAITING_SOURCE_GROUP", "WAITING_SPAM_INTERVAL", "WAITING_ROTATION_INTERVAL", "SPAM_READY"

########################################
# Keyboards
########################################

def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Weiter ▶️", callback_data="continue")]
    ])

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Аккаунты", callback_data="menu_accounts")],
        [
            InlineKeyboardButton("Tagger starten 🚀", callback_data="launch_tagger"),
            InlineKeyboardButton("Tagger stoppen 🛑", callback_data="stop_tagger")
        ],
        [InlineKeyboardButton("Anleitung 📚", callback_data="instructions")],
    ])

def accounts_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Аккаунт №1", callback_data="account_1")],
        [InlineKeyboardButton("Аккаунт №2", callback_data="account_2")],
        [InlineKeyboardButton("<< Назад", callback_data="go_back_main_menu")]
    ])

def digit_keyboard(current_code=""):
    """Клавиатура для цифр (0-9, удалить, отправить)."""
    kb = [
        [
            InlineKeyboardButton("1", callback_data="digit_1"),
            InlineKeyboardButton("2", callback_data="digit_2"),
            InlineKeyboardButton("3", callback_data="digit_3")
        ],
        [
            InlineKeyboardButton("4", callback_data="digit_4"),
            InlineKeyboardButton("5", callback_data="digit_5"),
            InlineKeyboardButton("6", callback_data="digit_6")
        ],
        [
            InlineKeyboardButton("7", callback_data="digit_7"),
            InlineKeyboardButton("8", callback_data="digit_8"),
            InlineKeyboardButton("9", callback_data="digit_9")
        ],
        [
            InlineKeyboardButton("0", callback_data="digit_0"),
            InlineKeyboardButton("Л⬅️", callback_data="digit_del"),
            InlineKeyboardButton("OK✅", callback_data="digit_submit")
        ]
    ]
    return InlineKeyboardMarkup(kb)

########################################
# /start
########################################
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    USER_STATE[user_id] = "MAIN_MENU"

    # Инициализируем структуру хранения аккаунтов, если ещё нет
    if 'accounts' not in context.user_data:
        context.user_data['accounts'] = {
            1: {'client': None, 'api_id': None, 'api_hash': None, 'phone': None, 'is_authorized': False},
            2: {'client': None, 'api_id': None, 'api_hash': None, 'phone': None, 'is_authorized': False},
        }

    await update.message.reply_text(
        "Привет! Нажми 'Weiter', чтобы увидеть меню:",
        reply_markup=start_keyboard()
    )

########################################
# Хелпер: получить последнее НЕ сервисное сообщение
########################################
async def get_last_non_service_message(client: TelegramClient, source_group: str):
    """
    Возвращает последнее (самое свежее) "обычное" (text/media) сообщение
    из source_group. Сервисные сообщения (msg.action != None) пропускаем.
    Возвращаем None, если ничего нет.
    """
    entity = await client.get_entity(source_group)
    raw_msgs = await client.get_messages(entity, limit=10)
    for m in raw_msgs:
        # Если нет m.action => это обычное сообщение
        if not m.action:
            return m
    return None

########################################
# Основная функция спама (два аккаунта, чередование)
########################################
async def run_tagger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Чередуем аккаунты №1 и №2 каждые rotation_interval секунд.  
       Каждые spam_interval секунд пересылаем последнее "обычное" сообщение из source_group.
       Сервисные сообщения пропускаем.
    """
    user_id = update.effective_user.id

    source_group = context.user_data.get('source_group')
    spam_interval = context.user_data.get('spam_interval', 60.0)
    rotation_interval = context.user_data.get('rotation_interval', 300.0)

    acc_data = context.user_data['accounts']
    client1 = acc_data[1]['client']
    client2 = acc_data[2]['client']

    if not (acc_data[1]['is_authorized'] and acc_data[2]['is_authorized']):
        await update.effective_message.reply_text(
            "Оба аккаунта не авторизованы! Настройте их через меню 'Аккаунты'."
        )
        return

    if not source_group:
        await update.effective_message.reply_text("Не указана группа-источник.")
        return

    stop_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Tagger stoppen 🛑", callback_data="stop_tagger")]
    ])

    await update.effective_message.reply_text(
        f"🚀 Запускаем рассылку!\n"
        f"Интервал отправки: {spam_interval} сек.\n"
        f"Переключение аккаунтов каждые: {rotation_interval} сек.\n",
        reply_markup=stop_keyboard
    )

    current_account = 1
    next_switch_time = time.time() + rotation_interval

    try:
        while True:
            try:
                # Выбираем активный аккаунт
                active_client = client1 if current_account == 1 else client2

                # Берём последнее НЕ сервисное сообщение
                last_msg = await get_last_non_service_message(active_client, source_group)
                if last_msg:
                    dialogs = await active_client.get_dialogs(limit=None)
                    target_chats = [d for d in dialogs if (d.is_group or d.is_channel)]

                    for chat in target_chats:
                        try:
                            await active_client.forward_messages(
                                entity=chat,
                                messages=last_msg,      # передаём сам объект
                                from_peer=last_msg.peer_id
                            )
                            logger.info(
                                f"[Акк {current_account}] Переслал msg_id={last_msg.id} "
                                f"в {chat.name or chat.id}"
                            )
                        except FloodWaitError as e:
                            logger.warning(f"[Акк {current_account}] FloodWait → {e.seconds} сек")
                            continue
                        except errors.ChatWriteForbiddenError:
                            logger.warning(f"[Акк {current_account}] Нет прав писать в {chat.name}. Пропускаем.")
                            continue
                        except errors.ChatAdminRequiredError:
                            logger.warning(f"[Акк {current_account}] Нужно быть админом в {chat.name}. Пропускаем.")
                            continue
                        except PeerIdInvalidError:
                            logger.warning(f"[Акк {current_account}] PeerIdInvalid для {chat.name}. Пропускаем.")
                            continue
                        except errors.rpcerrorlist.ChatIdInvalidError:
                            logger.warning(f"[Акк {current_account}] ChatIdInvalid для {chat.name}. Пропускаем.")
                            continue
                        except SessionPasswordNeededError:
                            logger.error(f"[Акк {current_account}] Нужен 2FA пароль!")
                            USER_STATE[user_id] = f"WAITING_PASSWORD_{current_account}"
                            return
                        except Exception as e:
                            logger.error(f"[Акк {current_account}] Ошибка при пересылке в {chat.name}: {e}")
                            continue

                # Пауза между рассылками
                await asyncio.sleep(spam_interval)

                # Проверяем, не пора ли переключить аккаунт
                if time.time() >= next_switch_time:
                    current_account = 2 if current_account == 1 else 1
                    next_switch_time = time.time() + rotation_interval
                    logger.info(f"Переключился на аккаунт №{current_account}")

            except asyncio.CancelledError:
                logger.info("Tagger остановлен (asyncio.CancelledError).")
                break
            except Exception as e:
                logger.error(f"Ошибка в основном цикле: {e}")
                await asyncio.sleep(5)

    finally:
        # При остановке - отключаемся
        if client1 and client1.is_connected():
            await client1.disconnect()
        if client2 and client2.is_connected():
            await client2.disconnect()

        USER_TAGGER_TASKS.pop(user_id, None)
        USER_STATE[user_id] = "MAIN_MENU"
        await update.effective_message.reply_text(
            "🛑 Tagger остановлен.",
            reply_markup=main_menu_keyboard()
        )

########################################
# CALLBACK-Handler
########################################
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    await query.answer()

    if data == "continue":
        USER_STATE[user_id] = "MAIN_MENU"
        await query.edit_message_text("Главное меню:", reply_markup=main_menu_keyboard())

    elif data == "menu_accounts":
        USER_STATE[user_id] = "CHOOSE_ACCOUNT"
        await query.edit_message_text("Выберите аккаунт:", reply_markup=accounts_menu_keyboard())

    elif data == "go_back_main_menu":
        USER_STATE[user_id] = "MAIN_MENU"
        await query.edit_message_text("Главное меню:", reply_markup=main_menu_keyboard())

    elif data == "account_1":
        USER_STATE[user_id] = "ENTER_API_ID_1"
        await query.edit_message_text("Введите API ID (число) для аккаунта №1:")

    elif data == "account_2":
        USER_STATE[user_id] = "ENTER_API_ID_2"
        await query.edit_message_text("Введите API ID (число) для аккаунта №2:")

    elif data == "launch_tagger":
        USER_STATE[user_id] = "WAITING_SOURCE_GROUP"
        await query.edit_message_text("Укажите @ссылку или username группы-источника:")

    elif data == "stop_tagger":
        task = USER_TAGGER_TASKS.get(user_id)
        if task and not task.done():
            task.cancel()
        else:
            await query.edit_message_text(
                "Tagger не запущен.",
                reply_markup=main_menu_keyboard()
            )

    elif data == "instructions":
        text_instructions = (
            "1) Откройте «Аккаунты» и настройте оба аккаунта.\n"
            "2) Запустите Tagger, указав группу-источник, интервал рассылки, интервал переключения.\n"
            "3) Бот пересылает последние 'обычные' сообщения (пропуская сервисные)."
        )
        await query.edit_message_text(text_instructions, reply_markup=main_menu_keyboard())

    # Обработка кнопок для ввода кода
    elif data.startswith("digit_"):
        action = data.split("_")[1]
        state = USER_STATE.get(user_id, "")

        # Определяем, для какого аккаунта идёт ввод кода
        if "WAITING_CODE_1" in state:
            acc_number = 1
        elif "WAITING_CODE_2" in state:
            acc_number = 2
        else:
            await query.answer("Неожиданный ввод кода.", show_alert=True)
            return

        current_code = context.user_data.get(f'code_{acc_number}', '')

        if action.isdigit():
            if len(current_code) < 6:
                current_code += action
                context.user_data[f'code_{acc_number}'] = current_code
            else:
                await query.answer("Макс длина кода 6", show_alert=True)
        elif action == "del":
            current_code = current_code[:-1]
            context.user_data[f'code_{acc_number}'] = current_code
        elif action == "submit":
            await confirm_code(update, context, acc_number)
            return

        masked_code = '*' * len(current_code) + '_' * (6 - len(current_code))
        await query.edit_message_text(
            f"Аккаунт №{acc_number}. Введите код: {masked_code}",
            reply_markup=digit_keyboard(current_code)
        )

########################################
# TEXT-Handler
########################################
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Гарантируем, что 'accounts' есть
    if 'accounts' not in context.user_data:
        context.user_data['accounts'] = {
            1: {'client': None, 'api_id': None, 'api_hash': None, 'phone': None, 'is_authorized': False},
            2: {'client': None, 'api_id': None, 'api_hash': None, 'phone': None, 'is_authorized': False},
        }

    state = USER_STATE.get(user_id, "")

    # Аккаунт №1
    if state == "ENTER_API_ID_1":
        if not update.message.text.strip().isdigit():
            await update.message.reply_text("Введите число (API ID):")
            return
        acc_data = context.user_data['accounts'][1]
        acc_data['api_id'] = int(update.message.text.strip())
        USER_STATE[user_id] = "ENTER_API_HASH_1"
        await update.message.reply_text("Введите API Hash для аккаунта №1:")
        return

    if state == "ENTER_API_HASH_1":
        acc_data = context.user_data['accounts'][1]
        acc_data['api_hash'] = update.message.text.strip()
        USER_STATE[user_id] = "ENTER_PHONE_1"
        await update.message.reply_text("Введите телефон (формат +9999999999) для аккаунта №1:")
        return

    if state == "ENTER_PHONE_1":
        phone = update.message.text.strip()
        if not phone.startswith('+') or not phone[1:].isdigit():
            await update.message.reply_text("Формат телефона: +123456789")
            return
        acc_data = context.user_data['accounts'][1]
        acc_data['phone'] = phone
        USER_STATE[user_id] = "WAITING_CODE_1"
        await update.message.reply_text("Запрашиваю код у Telegram...")
        await create_telethon_client(update, context, acc_number=1)
        return

    # Аккаунт №2
    if state == "ENTER_API_ID_2":
        if not update.message.text.strip().isdigit():
            await update.message.reply_text("Введите число (API ID):")
            return
        acc_data = context.user_data['accounts'][2]
        acc_data['api_id'] = int(update.message.text.strip())
        USER_STATE[user_id] = "ENTER_API_HASH_2"
        await update.message.reply_text("Введите API Hash для аккаунта №2:")
        return

    if state == "ENTER_API_HASH_2":
        acc_data = context.user_data['accounts'][2]
        acc_data['api_hash'] = update.message.text.strip()
        USER_STATE[user_id] = "ENTER_PHONE_2"
        await update.message.reply_text("Введите телефон (формат +9999999999) для аккаунта №2:")
        return

    if state == "ENTER_PHONE_2":
        phone = update.message.text.strip()
        if not phone.startswith('+') or not phone[1:].isdigit():
            await update.message.reply_text("Формат телефона: +123456789")
            return
        acc_data = context.user_data['accounts'][2]
        acc_data['phone'] = phone
        USER_STATE[user_id] = "WAITING_CODE_2"
        await update.message.reply_text("Запрашиваю код у Telegram...")
        await create_telethon_client(update, context, acc_number=2)
        return

    # 2FA пароль акк №1
    if state == "WAITING_PASSWORD_1":
        pw = update.message.text.strip()
        acc_data = context.user_data['accounts'][1]
        client = acc_data['client']
        if not client:
            await update.message.reply_text("Клиент не инициализирован. Повторите заново.")
            return
        try:
            await client.sign_in(password=pw)
            acc_data['is_authorized'] = True
            USER_STATE[user_id] = "MAIN_MENU"
            await update.message.reply_text(
                "Аккаунт №1 успешно авторизован!",
                reply_markup=main_menu_keyboard()
            )
        except errors.PasswordHashInvalidError:
            await update.message.reply_text("Неверный пароль. Попробуйте снова.")
        except FloodWaitError as e:
            await update.message.reply_text(f"Слишком много попыток. Подождите {e.seconds} сек.")
            USER_STATE[user_id] = "MAIN_MENU"
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}")
        return

    # 2FA пароль акк №2
    if state == "WAITING_PASSWORD_2":
        pw = update.message.text.strip()
        acc_data = context.user_data['accounts'][2]
        client = acc_data['client']
        if not client:
            await update.message.reply_text("Клиент не инициализирован. Повторите заново.")
            return
        try:
            await client.sign_in(password=pw)
            acc_data['is_authorized'] = True
            USER_STATE[user_id] = "MAIN_MENU"
            await update.message.reply_text(
                "Аккаунт №2 успешно авторизован!",
                reply_markup=main_menu_keyboard()
            )
        except errors.PasswordHashInvalidError:
            await update.message.reply_text("Неверный пароль. Попробуйте снова.")
        except FloodWaitError as e:
            await update.message.reply_text(f"Слишком много попыток. Подождите {e.seconds} сек.")
            USER_STATE[user_id] = "MAIN_MENU"
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}")
        return

    # Группа-источник
    if state == "WAITING_SOURCE_GROUP":
        source_group = update.message.text.strip()
        if not source_group:
            await update.message.reply_text("Введите корректную ссылку/username группы.")
            return
        context.user_data['source_group'] = source_group
        USER_STATE[user_id] = "WAITING_SPAM_INTERVAL"
        await update.message.reply_text("Введите интервал рассылки (сек), например 60:")
        return

    if state == "WAITING_SPAM_INTERVAL":
        try:
            val = float(update.message.text.strip())
            if val <= 0:
                raise ValueError("Интервал должен быть > 0.")
            context.user_data['spam_interval'] = val
            USER_STATE[user_id] = "WAITING_ROTATION_INTERVAL"
            await update.message.reply_text("Теперь введите интервал переключения аккаунтов (сек), например 300:")
        except ValueError:
            await update.message.reply_text("Нужно положительное число. Попробуйте ещё раз.")
        return

    if state == "WAITING_ROTATION_INTERVAL":
        try:
            val = float(update.message.text.strip())
            if val <= 0:
                raise ValueError("Интервал должен быть > 0.")
            context.user_data['rotation_interval'] = val
            await update.message.reply_text("Настройки приняты! Запускаю спам...")
            USER_STATE[user_id] = "SPAM_READY"
            task = asyncio.create_task(run_tagger(update, context))
            USER_TAGGER_TASKS[user_id] = task
        except ValueError:
            await update.message.reply_text("Нужно положительное число. Попробуйте ещё раз.")
        return

    await update.message.reply_text("Неизвестная команда. Используйте меню.")

########################################
# confirm_code - подтверждение кода
########################################
async def confirm_code(update: Update, context: ContextTypes.DEFAULT_TYPE, acc_number: int):
    user_id = update.effective_user.id
    code = context.user_data.get(f'code_{acc_number}', '')
    if not code:
        await update.effective_message.reply_text("Код пуст. Введите заново.")
        return

    acc_data = context.user_data['accounts'][acc_number]
    client = acc_data['client']
    if not client:
        await update.effective_message.reply_text("Клиент не инициализирован. Начните заново.")
        return

    phone_number = acc_data['phone']
    try:
        await client.sign_in(phone_number, code)
    except SessionPasswordNeededError:
        USER_STATE[user_id] = f"WAITING_PASSWORD_{acc_number}"
        await update.effective_message.reply_text("У вас включён 2FA. Введите пароль (сообщением).")
        return
    except FloodWaitError as e:
        await update.effective_message.reply_text(f"Слишком много попыток. Подождите {e.seconds} сек.")
        USER_STATE[user_id] = "MAIN_MENU"
        return
    except errors.PhoneCodeInvalidError:
        await update.effective_message.reply_text("Неверный код. Повторите ввод.")
        context.user_data[f'code_{acc_number}'] = ""
        await update.effective_message.reply_text(
            f"Аккаунт №{acc_number}. Введите код:",
            reply_markup=digit_keyboard()
        )
        USER_STATE[user_id] = f"WAITING_CODE_{acc_number}"
        return
    except Exception as e:
        await update.effective_message.reply_text(f"Ошибка при вводе кода: {e}")
        return

    acc_data['is_authorized'] = True
    USER_STATE[user_id] = "MAIN_MENU"
    await update.effective_message.reply_text(
        f"Аккаунт №{acc_number} успешно авторизован!",
        reply_markup=main_menu_keyboard()
    )

########################################
# Создание/подключение Telethon-клиента
########################################
async def create_telethon_client(update: Update, context: ContextTypes.DEFAULT_TYPE, acc_number: int):
    acc_data = context.user_data['accounts'][acc_number]
    api_id = acc_data['api_id']
    api_hash = acc_data['api_hash']
    phone_number = acc_data['phone']

    if not api_id or not api_hash or not phone_number:
        await update.message.reply_text("API-данные не полные. Начните заново.")
        return

    session_name = f"session_user_{update.effective_user.id}_acc_{acc_number}"
    if not acc_data['client']:
        client = TelegramClient(session_name, api_id, api_hash)
        acc_data['client'] = client
        await client.connect()
    else:
        client = acc_data['client']
        if not client.is_connected():
            await client.connect()

    try:
        if not await client.is_user_authorized():
            await client.send_code_request(phone_number)
            context.user_data[f'code_{acc_number}'] = ""
            USER_STATE[update.effective_user.id] = f"WAITING_CODE_{acc_number}"
            await update.message.reply_text(
                f"Аккаунт №{acc_number}. Введите код из Telegram:",
                reply_markup=digit_keyboard()
            )
        else:
            acc_data['is_authorized'] = True
            USER_STATE[update.effective_user.id] = "MAIN_MENU"
            await update.message.reply_text(
                f"Аккаунт №{acc_number} уже авторизован!",
                reply_markup=main_menu_keyboard()
            )
    except FloodWaitError as e:
        await update.message.reply_text(f"FloodWaitError: подождите {e.seconds} сек.")
        USER_STATE[update.effective_user.id] = "MAIN_MENU"
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
        USER_STATE[update.effective_user.id] = "MAIN_MENU"

########################################
# MAIN
########################################
if __name__ == "__main__":
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    application.run_polling()
