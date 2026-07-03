#!/usr/bin/env python3
"""
ArTPart42 Shop Bot
Telegram бот для магазинов + HTTP API для приёма заявок
MongoDB версия + aiogram 3.x
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import JSONResponse
import uvicorn

from database import init_db, execute_query, execute_query_one, test_connection

# Загружаем переменные окружения
load_dotenv()

# === КОНФИГУРАЦИЯ ===
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
API_KEY = os.getenv('API_KEY', 'default-secret-key-change-me')
PORT = int(os.getenv('PORT', '8000'))

# Тарифы
DEFAULT_TEST_DAYS = int(os.getenv('DEFAULT_TEST_DAYS', '30'))
PRICE_PER_REQUEST = int(os.getenv('PRICE_PER_REQUEST', '100'))
SUBSCRIPTION_PRICE = int(os.getenv('SUBSCRIPTION_PRICE', '3000'))
DEPOSIT_WARNING_THRESHOLD = int(os.getenv('DEPOSIT_WARNING_THRESHOLD', '300'))
MIN_DEPOSIT = int(os.getenv('MIN_DEPOSIT', '1000'))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === TELEGRAM БОТ ===
bot = Bot(token=TOKEN)
storage = MemoryStorage()
router = Router()
dp = Dispatcher(storage=storage)
dp.include_router(router)

# === СОСТОЯНИЯ ===
class ShopRegistration(StatesGroup):
    waiting_for_name = State()
    waiting_for_city = State()
    waiting_for_phone = State()
    waiting_for_email = State()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

def log_transaction(shop_id, tx_type, amount, balance_after, description, request_id=None):
    """Записывает транзакцию"""
    execute_query('transactions', {}, {
        'shop_id': shop_id,
        'type': tx_type,
        'amount': amount,
        'balance_after': balance_after,
        'description': description,
        'request_id': request_id,
        'created_at': datetime.now().isoformat()
    }, insert=True)

async def notify_admin_about_payment(shop, tx_type, amount, description):
    """Уведомляет админа о платеже"""
    if not ADMIN_CHAT_ID:
        return
    
    emoji = "💰" if tx_type == "deposit_topup" else "💳" if tx_type == "subscription_payment" else "💸"
    
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"{emoji} <b>Уведомление о платеже</b>\n\n"
            f"🏢 Магазин: {shop['name']} ({shop['city']})\n"
            f"👤 Chat ID: {shop['chat_id']}\n"
            f"💵 Сумма: {amount}₽\n"
            f"📝 {description}\n"
            f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить админа: {e}")

def get_tariff_display(shop):
    """Информация о тарифе для магазина (скрыт на тесте)"""
    if shop['monetization_type'] == 'test':
        return None
    
    elif shop['monetization_type'] == 'fixed':
        requests_left = shop['deposit_balance'] // PRICE_PER_REQUEST
        return (
            f"💰 <b>Тариф: Фикс за заявку</b>\n\n"
            f"💵 Стоимость: {PRICE_PER_REQUEST}₽ за обработанную заявку\n"
            f"💳 Баланс депозита: {shop['deposit_balance']}₽\n"
            f"📊 Обработано заявок: {requests_left}\n\n"
            f"⚠️ Обработанной считается заявка, по которой вы нажали «Показать телефон клиента»"
        )
    
    elif shop['monetization_type'] == 'subscription':
        if shop.get('subscription_end'):
            try:
                end_date = datetime.fromisoformat(shop['subscription_end'])
                days_left = (end_date - datetime.now()).days
                if days_left > 0:
                    return (
                        f"💳 <b>Тариф: Подписка</b>\n\n"
                        f"💵 Стоимость: {SUBSCRIPTION_PRICE}₽/мес\n"
                        f"📅 Действует до: {end_date.strftime('%d.%m.%Y')}\n"
                        f"⏳ Осталось дней: {days_left}\n\n"
                        f"✅ Безлимитные заявки"
                    )
                else:
                    return "⚠️ <b>Подписка закончилась</b>\n\nДля продления свяжитесь с @ArTPart42admin"
            except:
                pass
        return f"💳 <b>Тариф: Подписка</b>\n\n💵 Стоимость: {SUBSCRIPTION_PRICE}₽/мес"
    
    return None

def get_tariff_display_admin(shop):
    """Полная информация для админа"""
    if shop['monetization_type'] == 'test':
        if shop.get('test_period_end'):
            try:
                end_date = datetime.fromisoformat(shop['test_period_end'])
                days_left = (end_date - datetime.now()).days
                if days_left > 0:
                    return f"🎁 ТЕСТ (до {end_date.strftime('%d.%m.%Y')}, {days_left} дн.)"
                return "⚠️ ТЕСТ ЗАКОНЧИЛСЯ"
            except:
                pass
        return "🎁 ТЕСТ"
    
    elif shop['monetization_type'] == 'fixed':
        return f"💰 ФИКС | Депозит: {shop['deposit_balance']}₽ | {PRICE_PER_REQUEST}₽/заявка"
    
    elif shop['monetization_type'] == 'subscription':
        if shop.get('subscription_end'):
            try:
                end_date = datetime.fromisoformat(shop['subscription_end'])
                days_left = (end_date - datetime.now()).days
                if days_left > 0:
                    return f"💳 ПОДПИСКА | до {end_date.strftime('%d.%m.%Y')} ({days_left} дн.) | {SUBSCRIPTION_PRICE}₽/мес"
                return "⚠️ ПОДПИСКА ЗАКОНЧИЛАСЬ"
            except:
                pass
        return f"💳 ПОДПИСКА | {SUBSCRIPTION_PRICE}₽/мес"
    
    return "❓ НЕ ВЫБРАН"


# === КОМАНДЫ МАГАЗИНА ===

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Приветствие"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Мои заявки"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="💳 Мой тариф"), KeyboardButton(text="🏢 Регистрация магазина")],
            [KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True
    )
    
    await message.answer(
        "👋 Добро пожаловать в ArTPart42 Shop Bot!\n\n"
        "Этот бот помогает магазинам автозапчастей получать заявки от клиентов.\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message):
    """Справка"""
    await message.answer(
        "📖 <b>Как это работает:</b>\n\n"
        "1️⃣ Клиент оставляет заявку на сайте artpart42.ru\n"
        "2️⃣ Заявка автоматически приходит вам в бот\n"
        "3️⃣ Вы нажимаете [📞 Показать телефон] — это считается обработкой заявки\n"
        "4️⃣ Связываетесь с клиентом напрямую\n\n"
        "<b>Команды:</b>\n"
        "/start - Главное меню\n"
        "/stats - Ваша статистика\n"
        "/tariff - Информация о вашем тарифе\n"
        "/register - Регистрация нового магазина\n"
        "/help - Эта справка\n\n"
        "По всем вопросам: @ArTPart42admin"
    )

@router.message(Command("register"))
@router.message(F.text == "🏢 Регистрация магазина")
async def cmd_register(message: Message):
    """Регистрация"""
    shop = execute_query_one('shops', {'chat_id': message.chat.id})
    
    if shop:
        await message.answer(
            f"⚠️ Ваш магазин уже зарегистрирован:\n"
            f"🏢 {shop['name']}\n"
            f"📍 {shop['city']}\n\n"
            f"Если нужно изменить данные - напишите @ArTPart42admin"
        )
        return
    
    await message.answer("🏢 <b>Регистрация магазина</b>\n\nВведите название магазина:")
    await ShopRegistration.waiting_for_name.set()

@router.message(ShopRegistration.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("📍 Введите город:")
    await ShopRegistration.waiting_for_city.set()

@router.message(ShopRegistration.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    await state.update_data(city=message.text)
    await message.answer("📞 Введите телефон для связи:")
    await ShopRegistration.waiting_for_phone.set()

@router.message(ShopRegistration.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("📧 Введите email (для связи):")
    await ShopRegistration.waiting_for_email.set()

@router.message(ShopRegistration.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    data = await state.get_data()
    test_end = datetime.now() + timedelta(days=DEFAULT_TEST_DAYS)
    
    # Проверяем не зарегистрирован ли уже
    existing = execute_query_one('shops', {'chat_id': message.chat.id})
    if existing:
        await message.answer("⚠️ Ошибка: магазин уже зарегистрирован")
        await state.clear()
        return
    
    # Создаём магазин
    execute_query('shops', {}, {
        'name': data['name'],
        'city': data['city'],
        'chat_id': message.chat.id,
        'phone': data['phone'],
        'email': message.text,
        'is_active': 1,
        'monetization_type': 'test',
        'deposit_balance': 0,
        'test_period_end': test_end.isoformat(),
        'created_at': datetime.now().isoformat()
    }, insert=True)
    
    await state.clear()
    
    await message.answer(
        f"✅ <b>Магазин зарегистрирован!</b>\n\n"
        f"🏢 {data['name']}\n"
        f"📍 {data['city']}\n"
        f"📞 {data['phone']}\n"
        f"📧 {message.text}\n\n"
        f"Теперь вы будете получать заявки от клиентов.\n"
        f"Для выбора тарифа свяжитесь с администратором @ArTPart42admin"
    )
    
    # Уведомляем админа
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"🆕 <b>Новый магазин зарегистрирован</b>\n\n"
                f"🏢 {data['name']}\n"
                f"📍 {data['city']}\n"
                f"📞 {data['phone']}\n"
                f"📧 {message.text}\n"
                f"👤 Chat ID: {message.chat.id}\n"
                f"📅 Тест до: {test_end.strftime('%d.%m.%Y')}"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа: {e}")

@router.message(F.text == "💳 Мой тариф")
@router.message(Command("tariff"))
async def my_tariff(message: Message):
    """Информация о тарифе"""
    shop = execute_query_one('shops', {'chat_id': message.chat.id})
    
    if not shop:
        await message.answer("⚠️ Сначала зарегистрируйте магазин: /register")
        return
    
    tariff_info = get_tariff_display(shop)
    
    if tariff_info is None:
        await message.answer(
            f"💳 <b>Тариф</b>\n\n"
            f"🏢 {shop['name']}\n\n"
            f"Тариф не выбран.\n\n"
            f"Для выбора тарифа свяжитесь с администратором @ArTPart42admin\n\n"
            f"<b>Доступные тарифы:</b>\n"
            f"💰 <b>Фикс за заявку</b> — {PRICE_PER_REQUEST}₽ за обработанную заявку (депозит от {MIN_DEPOSIT}₽)\n"
            f"💳 <b>Подписка</b> — {SUBSCRIPTION_PRICE}₽/мес, безлимитные заявки"
        )
    else:
        await message.answer(
            f"💳 <b>Ваш тариф:</b>\n\n"
            f"🏢 {shop['name']}\n\n"
            f"{tariff_info}\n\n"
            f"По вопросам: @ArTPart42admin"
        )

@router.message(F.text == "📋 Мои заявки")
async def my_requests(message: Message):
    """Последние заявки"""
    shop = execute_query_one('shops', {'chat_id': message.chat.id})
    
    if not shop:
        await message.answer("⚠️ Сначала зарегистрируйте магазин: /register")
        return
    
    # Получаем shop_id
    shop_id = shop['_id']
    
    # Получаем последние 10 заявок
    shop_requests = execute_query('shop_requests', {'shop_id': shop_id}, fetch=True)
    shop_requests = sorted(shop_requests, key=lambda x: x.get('created_at', ''), reverse=True)[:10]
    
    if not shop_requests:
        await message.answer("📭 У вас пока нет заявок")
        return
    
    text = "📋 <b>Последние 10 заявок:</b>\n\n"
    for sr in shop_requests:
        # Получаем данные заявки
        request = execute_query_one('requests', {'_id': sr['request_id']})
        if not request:
            continue
        
        if sr.get('phone_shown'):
            status_emoji = "📞"
            status_text = "телефон показан"
        elif sr.get('status') == 'rejected':
            status_emoji = "❌"
            status_text = "нет в наличии"
        else:
            status_emoji = "⏳"
            status_text = "ожидает ответа"
        
        text += f"{status_emoji} #{request['_id']} | {request['car_brand']} {request['car_model']}\n"
        text += f"   📝 {request['part_name']}\n"
        text += f"   📅 {request['created_at'][:16]} | {status_text}\n\n"
    
    await message.answer(text)

@router.message(F.text == "📊 Статистика")
@router.message(Command("stats"))
async def stats(message: Message):
    """Статистика"""
    shop = execute_query_one('shops', {'chat_id': message.chat.id})
    
    if not shop:
        await message.answer("⚠️ Сначала зарегистрируйте магазин: /register")
        return
    
    shop_id = shop['_id']
    
    # Считаем статистику
    all_requests = execute_query('shop_requests', {'shop_id': shop_id}, fetch=True)
    total = len(all_requests)
    phone_shown = len([r for r in all_requests if r.get('phone_shown')])
    rejected = len([r for r in all_requests if r.get('status') == 'rejected'])
    pending = len([r for r in all_requests if r.get('status') == 'pending'])
    
    text = (
        f"📊 <b>Ваша статистика:</b>\n\n"
        f"🏢 {shop['name']} ({shop['city']})\n\n"
        f"📥 Всего заявок: {total}\n"
        f"📞 Показано телефонов: {phone_shown}\n"
        f"❌ Нет в наличии: {rejected}\n"
        f"⏳ Ожидают ответа: {pending}"
    )
    
    tariff_info = get_tariff_display(shop)
    if tariff_info:
        text += f"\n\n{tariff_info}"
    
    await message.answer(text)


# === CALLBACK ОБРАБОТЧИКИ ===

@router.callback_query(F.data.startswith('show_phone_'))
async def callback_show_phone(callback_query: CallbackQuery):
    """Показать телефон"""
    request_id = int(callback_query.data.split('_')[2])
    chat_id = callback_query.message.chat.id
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await callback_query.answer("❌ Магазин не найден", show_alert=True)
        return
    
    # Находим shop_request
    shop_request = execute_query_one('shop_requests', {
        'request_id': request_id,
        'shop_id': shop['_id']
    })
    
    if not shop_request:
        await callback_query.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    if shop_request.get('phone_shown'):
        await callback_query.answer("⚠️ Телефон уже показан", show_alert=True)
        return
    
    # Проверяем тариф
    if shop['monetization_type'] == 'test':
        # Тест — бесплатно
        pass
    
    elif shop['monetization_type'] == 'subscription':
        if shop.get('subscription_end'):
            try:
                end_date = datetime.fromisoformat(shop['subscription_end'])
                if end_date < datetime.now():
                    await callback_query.answer(
                        "⚠️ Подписка закончилась. Свяжитесь с @ArTPart42admin",
                        show_alert=True
                    )
                    return
            except:
                pass
    
    elif shop['monetization_type'] == 'fixed':
        if shop['deposit_balance'] < PRICE_PER_REQUEST:
            await callback_query.answer(
                f"⚠️ Недостаточно средств. Баланс: {shop['deposit_balance']}₽\n"
                f"Для пополнения свяжитесь с @ArTPart42admin",
                show_alert=True
            )
            return
        
        # Списываем с депозита
        new_balance = shop['deposit_balance'] - PRICE_PER_REQUEST
        from database import get_db
        db = get_db()
        db['shops'].update_one(
            {'_id': shop['_id']},
            {'$set': {'deposit_balance': new_balance}}
        )
        
        # Логируем транзакцию
        log_transaction(
            shop['_id'],
            'request_charge',
            -PRICE_PER_REQUEST,
            new_balance,
            f'Списание за заявку #{request_id}',
            request_id
        )
        
        # Обновляем shop_request
        db['shop_requests'].update_one(
            {'_id': shop_request['_id']},
            {'$set': {
                'phone_shown': 1,
                'phone_shown_at': datetime.now().isoformat(),
                'commission_charged': PRICE_PER_REQUEST,
                'status': 'responded'
            }}
        )
        
        # Предупреждение о низком балансе
        if new_balance < DEPOSIT_WARNING_THRESHOLD:
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ <b>Внимание!</b>\n\n"
                    f"На вашем депозите осталось {new_balance}₽.\n"
                    f"Для пополнения свяжитесь с @ArTPart42admin"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить предупреждение: {e}")
        
        # Уведомляем админа
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"💸 <b>Списание с депозита</b>\n\n"
                    f"🏢 {shop['name']}\n"
                    f"💵 Сумма: {PRICE_PER_REQUEST}₽\n"
                    f"💳 Остаток: {new_balance}₽\n"
                    f"📋 Заявка #{request_id}"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить админа: {e}")
    
    else:
        # Обновляем shop_request без списания
        from database import get_db
        db = get_db()
        db['shop_requests'].update_one(
            {'_id': shop_request['_id']},
            {'$set': {
                'phone_shown': 1,
                'phone_shown_at': datetime.now().isoformat(),
                'status': 'responded'
            }}
        )
    
    # Получаем телефон клиента
    request = execute_query_one('requests', {'_id': request_id})
    
    await callback_query.message.edit_text(
        f"✅ <b>Заявка #{request_id} обработана</b>\n\n"
        f"🚙 {request['car_brand']} {request['car_model']}\n"
        f"🔧 {request['part_name']}\n\n"
        f"📞 <b>Телефон клиента:</b> {request['client_phone']}\n"
        f"👤 {request.get('client_name', 'Не указан')}"
    )
    
    await callback_query.answer("✅ Телефон показан", show_alert=False)

@router.callback_query(F.data.startswith('reject_'))
async def callback_reject(callback_query: CallbackQuery):
    """Нет в наличии"""
    request_id = int(callback_query.data.split('_')[1])
    chat_id = callback_query.message.chat.id
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await callback_query.answer("❌ Магазин не найден", show_alert=True)
        return
    
    from database import get_db
    db = get_db()
    db['shop_requests'].update_one(
        {'request_id': request_id, 'shop_id': shop['_id']},
        {'$set': {'status': 'rejected'}}
    )
    
    await callback_query.message.edit_text(
        f"❌ <b>Заявка #{request_id}</b>\n\n"
        f"Отмечено: нет в наличии"
    )
    
    await callback_query.answer("Отмечено", show_alert=False)


# === АДМИНСКИЕ КОМАНДЫ ===
from database import get_db
from bson import ObjectId

@router.message(Command("admin"))
async def admin_panel(message: Message):
    """Админ-панель"""
    if message.chat.id != ADMIN_CHAT_ID:
        await message.answer("⛔ Доступ запрещён")
        return
    
    db = get_db()
    shops_count = db['shops'].count_documents({})
    requests_count = db['requests'].count_documents({})
    phone_shown_count = db['shop_requests'].count_documents({'phone_shown': 1})
    
    total_deposit = sum(s.get('deposit_balance', 0) for s in db['shops'].find({'monetization_type': 'fixed'}))
    
    total_income = sum(abs(tx.get('amount', 0)) for tx in db['transactions'].find({'type': 'request_charge'}))
    total_topups = sum(tx.get('amount', 0) for tx in db['transactions'].find({'type': 'deposit_topup'}))
    total_subs = sum(tx.get('amount', 0) for tx in db['transactions'].find({'type': 'subscription_payment'}))
    
    await message.answer(
        f"👑 <b>Админ-панель</b>\n\n"
        f"🏢 Магазинов: {shops_count}\n"
        f"📥 Всего заявок: {requests_count}\n"
        f"📞 Показано телефонов: {phone_shown_count}\n\n"
        f" <b>Финансы:</b>\n"
        f"💳 На депозитах: {total_deposit}₽\n"
        f"💵 Пополнений: {total_topups}₽\n"
        f"💳 Подписок: {total_subs}₽\n"
        f"💸 Списано за заявки: {total_income}₽\n\n"
        f"<b>Команды:</b>\n"
        f"/shops - список магазинов\n"
        f"/shop_info <chat_id> - детали\n"
        f"/add_deposit <chat_id> <amount> - пополнить\n"
        f"/set_fixed <chat_id> - тариф Фикс\n"
        f"/set_subscription <chat_id> <months> - подписка\n"
        f"/set_test <chat_id> <days> - тест\n"
        f"/notify_tariff <chat_id> - уведомить\n"
        f"/transactions <chat_id> - история"
    )

@router.message(Command("shops"))
async def list_shops(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    db = get_db()
    shops = list(db['shops'].find({'is_active': 1}))
    if not shops:
        await message.answer("📭 Магазинов пока нет")
        return
    text = "🏢 <b>Список магазинов:</b>\n\n"
    for shop in shops:
        text += f" {shop['name']} ({shop['city']})\n👤 Chat ID: <code>{shop['chat_id']}</code>\n💳 {get_tariff_display_admin(shop)}\n\n"
    await message.answer(text, parse_mode="HTML")

@router.message(Command("shop_info"))
async def shop_info(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /shop_info <chat_id>")
        return
    try: chat_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    db = get_db()
    all_requests = list(db['shop_requests'].find({'shop_id': shop['_id']}))
    total = len(all_requests)
    phone_shown = len([r for r in all_requests if r.get('phone_shown')])
    rejected = len([r for r in all_requests if r.get('status') == 'rejected'])
    
    text = (
        f"🏢 <b>{shop['name']}</b> ({shop['city']})\n"
        f"📞 {shop['phone']} |  {shop['email']}\n"
        f"👤 Chat ID: <code>{shop['chat_id']}</code>\n\n"
        f"📥 Всего: {total} |  Показано: {phone_shown} | ❌ Отказов: {rejected}\n\n"
        f"<b>Тариф:</b> {get_tariff_display_admin(shop)}\n"
    )
    if shop['monetization_type'] == 'fixed':
        text += f"💳 Баланс: {shop['deposit_balance']}₽\n"
    await message.answer(text, parse_mode="HTML")

@router.message(Command("add_deposit"))
async def add_deposit(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    args = message.text.split()
    if len(args) != 3:
        await message.answer("Использование: /add_deposit <chat_id> <amount>")
        return
    try:
        chat_id = int(args[1])
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    if shop['monetization_type'] != 'fixed':
        await message.answer(f"❌ Сначала переведите на тариф Фикс: /set_fixed {chat_id}")
        return
    
    new_balance = shop['deposit_balance'] + amount
    db = get_db()
    db['shops'].update_one({'_id': shop['_id']}, {'$set': {'deposit_balance': new_balance}})
    log_transaction(shop['_id'], 'deposit_topup', amount, new_balance, 'Пополнение депозита')
    
    await message.answer(f"✅ Депозит пополнен\n🏢 {shop['name']}\n +{amount}₽\n💳 Баланс: {new_balance}₽")
    try:
        await bot.send_message(chat_id, f"💰 <b>Депозит пополнен!</b>\n💵 +{amount}₽\n💳 Баланс: {new_balance}₽")
    except: pass

@router.message(Command("set_fixed"))
async def set_fixed(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /set_fixed <chat_id>")
        return
    try: chat_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    db = get_db()
    db['shops'].update_one({'_id': shop['_id']}, {'$set': {'monetization_type': 'fixed'}})
    await message.answer(f"✅ {shop['name']} переведён на тариф «Фикс»")

@router.message(Command("set_subscription"))
async def set_subscription(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    args = message.text.split()
    if len(args) != 3:
        await message.answer("Использование: /set_subscription <chat_id> <months>")
        return
    try:
        chat_id = int(args[1])
        months = int(args[2])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer(" Магазин не найден")
        return
    
    sub_end = datetime.now() + timedelta(days=30 * months)
    total_price = SUBSCRIPTION_PRICE * months
    
    db = get_db()
    db['shops'].update_one({'_id': shop['_id']}, {'$set': {'monetization_type': 'subscription', 'subscription_end': sub_end.isoformat()}})
    log_transaction(shop['_id'], 'subscription_payment', total_price, 0, f'Подписка на {months} мес.')
    
    await message.answer(f"✅ Подписка установлена\n {shop['name']}\n До: {sub_end.strftime('%d.%m.%Y')}")

@router.message(Command("set_test"))
async def set_test(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    args = message.text.split()
    if len(args) != 3:
        await message.answer("Использование: /set_test <chat_id> <days>")
        return
    try:
        chat_id = int(args[1])
        days = int(args[2])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    test_end = datetime.now() + timedelta(days=days)
    db = get_db()
    db['shops'].update_one({'_id': shop['_id']}, {'$set': {'monetization_type': 'test', 'test_period_end': test_end.isoformat()}})
    await message.answer(f"✅ Тест продлён на {days} дней для {shop['name']}")

@router.message(Command("notify_tariff"))
async def notify_tariff(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /notify_tariff <chat_id>")
        return
    try: chat_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    try:
        await bot.send_message(chat_id, f"📢 <b>Важное уведомление!</b>\n\nВаш тестовый период заканчивается. Для продолжения работы выберите тариф:\n\n💰 Фикс: {PRICE_PER_REQUEST}₽/заявка\n💳 Подписка: {SUBSCRIPTION_PRICE}₽/мес\n\nСвяжитесь с @ArTPart42admin")
        await message.answer(f"✅ Уведомление отправлено {shop['name']}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@router.message(Command("transactions"))
async def show_transactions(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /transactions <chat_id>")
        return
    try: chat_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    db = get_db()
    transactions = list(db['transactions'].find({'shop_id': shop['_id']}).sort('created_at', -1).limit(20))
    if not transactions:
        await message.answer(f"📭 У {shop['name']} нет транзакций")
        return
    
    text = f"💳 <b>История — {shop['name']}</b>\n\n"
    for tx in transactions:
        emoji = "" if tx['amount'] > 0 else "💸"
        text += f"{emoji} {tx['created_at'][:16]} | {tx['amount']}₽ | {tx['description']}\n"
    await message.answer(text)

# === ФУНКЦИЯ ОТПРАВКИ ЗАЯВОК ===

async def send_request_to_shops(request_data):
    """Отправляет заявку магазинам"""
    db = get_db()
    
    request_id = db['requests'].insert_one({
        'vin': request_data.get('vin'),
        'car_brand': request_data.get('car_brand'),
        'car_model': request_data.get('car_model'),
        'part_name': request_data.get('part_name'),
        'client_name': request_data.get('client_name'),
        'client_phone': request_data.get('client_phone'),
        'status': 'new',
        'created_at': datetime.now().isoformat()
    }).inserted_id
    
    shops = list(db['shops'].find({'is_active': 1}))
    sent_count = 0
    
    for shop in shops:
        can_receive = False
        if shop['monetization_type'] == 'test':
            can_receive = True
        elif shop['monetization_type'] == 'subscription':
            if shop.get('subscription_end'):
                try:
                    if datetime.fromisoformat(shop['subscription_end']) > datetime.now():
                        can_receive = True
                except: pass
        elif shop['monetization_type'] == 'fixed':
            if shop.get('deposit_balance', 0) >= PRICE_PER_REQUEST:
                can_receive = True
        
        if can_receive:
            db['shop_requests'].insert_one({
                'request_id': request_id,
                'shop_id': shop['_id'],
                'status': 'pending',
                'phone_shown': 0,
                'created_at': datetime.now().isoformat()
            })
            
            try:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📞 Показать телефон", callback_data=f"show_phone_{request_id}")],
                    [InlineKeyboardButton(text="❌ Нет в наличии", callback_data=f"reject_{request_id}")]
                ])
                
                text = f"🚗 <b>Новая заявка #{request_id}</b>\n\n"
                text += f" Авто: {request_data.get('car_brand')} {request_data.get('car_model')}\n"
                if request_data.get('vin'): text += f"🔑 VIN: {request_data.get('vin')}\n"
                text += f" Запчасть: {request_data.get('part_name')}\n"
                if request_data.get('client_name'): text += f"👤 Клиент: {request_data.get('client_name')}\n"
                text += "\nНажмите « Показать телефон» чтобы связаться с клиентом"
                
                await bot.send_message(shop['chat_id'], text, reply_markup=keyboard)
                sent_count += 1
            except Exception as e:
                logger.error(f"Не удалось отправить магазину {shop['chat_id']}: {e}")
    
    logger.info(f"Заявка #{request_id} отправлена в {sent_count} магазинов")
    return str(request_id), sent_count

# === FASTAPI ПРИЛОЖЕНИЕ ===

async def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return x_api_key

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Запуск ArTPart42 Shop Bot")
    if test_connection():
        logger.info("✅ Подключение к MongoDB установлено")
    else:
        logger.warning("⚠️ Не удалось подключиться к MongoDB")
    
    init_db()
    
    # Запускаем бота в фоне
    poll_task = asyncio.create_task(dp.start_polling(bot))
    
    yield
    
    poll_task.cancel()
    await bot.session.close()
    logger.info(" Остановка бота")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "ok", "service": "ArTPart42 Shop Bot"}

@app.get("/health")
async def health():
    db_ok = test_connection()
    return {"status": "ok" if db_ok else "degraded", "database": "connected" if db_ok else "disconnected"}

@app.post("/api/request")
async def create_request(request_data: dict, api_key: str = Depends(verify_api_key)):
    try:
        request_id, sent_count = await send_request_to_shops(request_data)
        return {"success": True, "request_id": request_id, "shops_notified": sent_count}
    except Exception as e:
        logger.error(f"Ошибка создания заявки: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats")
async def get_stats(api_key: str = Depends(verify_api_key)):
    db = get_db()
    return {
        "shops": db['shops'].count_documents({}),
        "requests": db['requests'].count_documents({}),
        "phone_shown": db['shop_requests'].count_documents({'phone_shown': 1})
    }

# === ЗАПУСК ===
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
