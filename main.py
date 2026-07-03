#!/usr/bin/env python3
"""
ArTPart42 Shop Bot
Telegram бот для магазинов + HTTP API для приёма заявок
MongoDB версия
"""

import os
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
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
dp = Dispatcher(bot, storage=storage)

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

def notify_admin_about_payment(shop, tx_type, amount, description):
    """Уведомляет админа о платеже"""
    if not ADMIN_CHAT_ID:
        return
    
    emoji = "💰" if tx_type == "deposit_topup" else "💳" if tx_type == "subscription_payment" else "💸"
    
    try:
        import asyncio
        asyncio.create_task(bot.send_message(
            ADMIN_CHAT_ID,
            f"{emoji} <b>Уведомление о платеже</b>\n\n"
            f"🏢 Магазин: {shop['name']} ({shop['city']})\n"
            f"👤 Chat ID: {shop['chat_id']}\n"
            f"💵 Сумма: {amount}₽\n"
            f"📝 {description}\n"
            f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        ))
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

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Приветствие"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(
        KeyboardButton("📋 Мои заявки"),
        KeyboardButton("📊 Статистика")
    )
    keyboard.add(
        KeyboardButton("💳 Мой тариф"),
        KeyboardButton("🏢 Регистрация магазина")
    )
    keyboard.add(KeyboardButton("ℹ️ Помощь"))
    
    await message.answer(
        "👋 Добро пожаловать в ArTPart42 Shop Bot!\n\n"
        "Этот бот помогает магазинам автозапчастей получать заявки от клиентов.\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@dp.message_handler(commands=['help'])
@dp.message_handler(lambda m: m.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
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

@dp.message_handler(commands=['register'])
@dp.message_handler(lambda m: m.text == "🏢 Регистрация магазина")
async def cmd_register(message: types.Message):
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

@dp.message_handler(state=ShopRegistration.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("📍 Введите город:")
    await ShopRegistration.waiting_for_city.set()

@dp.message_handler(state=ShopRegistration.waiting_for_city)
async def process_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text)
    await message.answer("📞 Введите телефон для связи:")
    await ShopRegistration.waiting_for_phone.set()

@dp.message_handler(state=ShopRegistration.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("📧 Введите email (для связи):")
    await ShopRegistration.waiting_for_email.set()

@dp.message_handler(state=ShopRegistration.waiting_for_email)
async def process_email(message: types.Message, state: FSMContext):
    data = await state.get_data()
    test_end = datetime.now() + timedelta(days=DEFAULT_TEST_DAYS)
    
    # Проверяем не зарегистрирован ли уже
    existing = execute_query_one('shops', {'chat_id': message.chat.id})
    if existing:
        await message.answer("⚠️ Ошибка: магазин уже зарегистрирован")
        await state.finish()
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
    
    await state.finish()
    
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

@dp.message_handler(lambda m: m.text == "💳 Мой тариф")
@dp.message_handler(commands=['tariff'])
async def my_tariff(message: types.Message):
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

@dp.message_handler(lambda m: m.text == "📋 Мои заявки")
async def my_requests(message: types.Message):
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

@dp.message_handler(lambda m: m.text == "📊 Статистика")
@dp.message_handler(commands=['stats'])
async def stats(message: types.Message):
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

@dp.callback_query_handler(lambda c: c.data.startswith('show_phone_'))
async def callback_show_phone(callback_query: types.CallbackQuery):
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

@dp.callback_query_handler(lambda c: c.data.startswith('reject_'))
async def callback_reject(callback_query: types.CallbackQuery):
    """Нет в наличии"""
    request_id = int(callback_query.data.split('_')[1])
    chat_id = callback_query.message.chat.id
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await callback_query.answer("❌ Магазин не найден", show_alert=True)
        return
    
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
# ВАЖНО: добавляем импорт get_db (используется в админ-командах)
from database import get_db
from bson import ObjectId

@dp.message_handler(commands=['admin'])
async def admin_panel(message: types.Message):
    """Админ-панель"""
    if message.chat.id != ADMIN_CHAT_ID:
        await message.answer("⛔ Доступ запрещён")
        return
    
    db = get_db()
    shops_count = db['shops'].count_documents({})
    requests_count = db['requests'].count_documents({})
    phone_shown_count = db['shop_requests'].count_documents({'phone_shown': 1})
    
    # Общая сумма на депозитах
    total_deposit = 0
    for shop in db['shops'].find({'monetization_type': 'fixed'}):
        total_deposit += shop.get('deposit_balance', 0)
    
    # Доход за всё время
    total_income = 0
    for tx in db['transactions'].find({'type': 'request_charge'}):
        total_income += abs(tx.get('amount', 0))
    
    total_topups = 0
    for tx in db['transactions'].find({'type': 'deposit_topup'}):
        total_topups += tx.get('amount', 0)
    
    total_subs = 0
    for tx in db['transactions'].find({'type': 'subscription_payment'}):
        total_subs += tx.get('amount', 0)
    
    await message.answer(
        f"👑 <b>Админ-панель</b>\n\n"
        f"🏢 Магазинов: {shops_count}\n"
        f"📥 Всего заявок: {requests_count}\n"
        f"📞 Показано телефонов: {phone_shown_count}\n\n"
        f"💰 <b>Финансы:</b>\n"
        f"💳 На депозитах: {total_deposit}₽\n"
        f"💵 Пополнений: {total_topups}₽\n"
        f"💳 Подписок: {total_subs}₽\n"
        f"💸 Списано за заявки: {total_income}₽\n\n"
        f"<b>Команды:</b>\n"
        f"/shops - список магазинов\n"
        f"/shop_info &lt;chat_id&gt; - детали\n"
        f"/add_deposit &lt;chat_id&gt; &lt;amount&gt; - пополнить\n"
        f"/set_fixed &lt;chat_id&gt; - тариф Фикс\n"
        f"/set_subscription &lt;chat_id&gt; &lt;months&gt; - подписка\n"
        f"/set_test &lt;chat_id&gt; &lt;days&gt; - тест\n"
        f"/notify_tariff &lt;chat_id&gt; - уведомить\n"
        f"/transactions &lt;chat_id&gt; - история"
    )

@dp.message_handler(commands=['shops'])
async def list_shops(message: types.Message):
    """Список магазинов"""
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
    db = get_db()
    shops = list(db['shops'].find({'is_active': 1}))
    
    if not shops:
        await message.answer("📭 Магазинов пока нет")
        return
    
    text = "🏢 <b>Список магазинов:</b>\n\n"
    for shop in shops:
        tariff = get_tariff_display_admin(shop)
        text += f"🏢 {shop['name']} ({shop['city']})\n"
        text += f"   👤 Chat ID: <code>{shop['chat_id']}</code>\n"
        text += f"   💳 {tariff}\n\n"
    
    await message.answer(text, parse_mode="HTML")

@dp.message_handler(commands=['shop_info'])
async def shop_info(message: types.Message):
    """Детали магазина"""
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /shop_info <chat_id>")
        return
    
    try:
        chat_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    db = get_db()
    shop_id = shop['_id']
    
    all_requests = list(db['shop_requests'].find({'shop_id': shop_id}))
    total = len(all_requests)
    phone_shown = len([r for r in all_requests if r.get('phone_shown')])
    rejected = len([r for r in all_requests if r.get('status') == 'rejected'])
    pending = len([r for r in all_requests if r.get('status') == 'pending'])
    
    transactions = list(db['transactions'].find(
        {'shop_id': shop_id}
    ).sort('created_at', -1).limit(5))
    
    tariff = get_tariff_display_admin(shop)
    
    text = (
        f"🏢 <b>Информация о магазине:</b>\n\n"
        f"🏢 {shop['name']}\n"
        f"📍 {shop['city']}\n"
        f"📞 {shop['phone']}\n"
        f"📧 {shop['email']}\n"
        f"👤 Chat ID: <code>{shop['chat_id']}</code>\n\n"
        f"<b>Статистика:</b>\n"
        f"📥 Всего заявок: {total}\n"
        f"📞 Показано телефонов: {phone_shown}\n"
        f"❌ Отказов: {rejected}\n"
        f"⏳ Ожидают: {pending}\n\n"
        f"<b>Тариф:</b> {tariff}\n"
    )
    
    if shop['monetization_type'] == 'fixed':
        text += f"💳 Баланс депозита: {shop['deposit_balance']}₽\n"
    
    if transactions:
        text += f"\n<b>Последние операции:</b>\n"
        for tx in transactions:
            emoji = "💰" if tx['amount'] > 0 else "💸"
            text += f"{emoji} {tx['created_at'][:16]} | {tx['amount']}₽ | {tx['description']}\n"
    
    await message.answer(text, parse_mode="HTML")

@dp.message_handler(commands=['add_deposit'])
async def add_deposit(message: types.Message):
    """Пополнить депозит"""
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
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
    
    if amount <= 0:
        await message.answer("❌ Сумма должна быть положительной")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    if shop['monetization_type'] != 'fixed':
        await message.answer(f"❌ У магазина не тариф «Фикс». Сначала /set_fixed {chat_id}")
        return
    
    new_balance = shop['deposit_balance'] + amount
    
    db = get_db()
    db['shops'].update_one(
        {'_id': shop['_id']},
        {'$set': {'deposit_balance': new_balance}}
    )
    
    log_transaction(shop['_id'], 'deposit_topup', amount, new_balance, 'Пополнение депозита')
    
    await message.answer(
        f"✅ Депозит пополнен\n\n"
        f"🏢 {shop['name']}\n"
        f"💵 Пополнение: {amount}₽\n"
        f"💳 Новый баланс: {new_balance}₽"
    )
    
    # Уведомляем магазин
    try:
        await bot.send_message(
            chat_id,
            f"💰 <b>Депозит пополнен!</b>\n\n"
            f"💵 Сумма: {amount}₽\n"
            f"💳 Новый баланс: {new_balance}₽\n\n"
            f"Продолжайте получать заявки!"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить магазин: {e}")
    
    # Уведомляем админа о пополнении
    notify_admin_about_payment(shop, 'deposit_topup', amount, 'Пополнение депозита')

@dp.message_handler(commands=['set_fixed'])
async def set_fixed(message: types.Message):
    """Тариф Фикс"""
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /set_fixed <chat_id>")
        return
    
    try:
        chat_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    db = get_db()
    db['shops'].update_one(
        {'_id': shop['_id']},
        {'$set': {'monetization_type': 'fixed'}}
    )
    
    await message.answer(f"✅ Магазин {shop['name']} переведён на тариф «Фикс»")
    
    try:
        await bot.send_message(
            chat_id,
            f"💳 <b>Ваш тариф активирован!</b>\n\n"
            f"💰 <b>Тариф: Фикс за заявку</b>\n"
            f"💵 Стоимость: {PRICE_PER_REQUEST}₽ за обработанную заявку\n"
            f"💳 Баланс депозита: {shop['deposit_balance']}₽\n\n"
            f"Для пополнения свяжитесь с @ArTPart42admin\n"
            f"Минимальная сумма: {MIN_DEPOSIT}₽"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить магазин: {e}")

@dp.message_handler(commands=['set_subscription'])
async def set_subscription(message: types.Message):
    """Подписка"""
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
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
    
    if months <= 0:
        await message.answer("❌ Количество месяцев должно быть положительным")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    sub_end = datetime.now() + timedelta(days=30 * months)
    total_price = SUBSCRIPTION_PRICE * months
    
    db = get_db()
    db['shops'].update_one(
        {'_id': shop['_id']},
        {'$set': {
            'monetization_type': 'subscription',
            'subscription_end': sub_end.isoformat()
        }}
    )
    
    log_transaction(
        shop['_id'], 'subscription_payment', total_price, 0,
        f'Оплата подписки на {months} мес. (до {sub_end.strftime("%d.%m.%Y")})'
    )
    
    await message.answer(
        f"✅ Подписка установлена\n\n"
        f"🏢 {shop['name']}\n"
        f"💳 На {months} мес.\n"
        f"💵 Сумма: {total_price}₽\n"
        f"📅 До: {sub_end.strftime('%d.%m.%Y')}"
    )
    
    try:
        await bot.send_message(
            chat_id,
            f"💳 <b>Ваш тариф активирован!</b>\n\n"
            f"💳 <b>Тариф: Подписка</b>\n"
            f"💵 Стоимость: {SUBSCRIPTION_PRICE}₽/мес\n"
            f"📅 Действует до: {sub_end.strftime('%d.%m.%Y')}\n"
            f"⏳ Оплачено: {months} мес.\n\n"
            f"✅ Безлимитные заявки!"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить магазин: {e}")

@dp.message_handler(commands=['set_test'])
async def set_test(message: types.Message):
    """Продлить тест"""
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
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
    db['shops'].update_one(
        {'_id': shop['_id']},
        {'$set': {
            'monetization_type': 'test',
            'test_period_end': test_end.isoformat()
        }}
    )
    
    await message.answer(f"✅ Тест продлён на {days} дней для {shop['name']}")

@dp.message_handler(commands=['notify_tariff'])
async def notify_tariff(message: types.Message):
    """Уведомить о тарифе"""
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /notify_tariff <chat_id>")
        return
    
    try:
        chat_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    try:
        await bot.send_message(
            chat_id,
            f"📢 <b>Важное уведомление!</b>\n\n"
            f"Уважаемый партнёр!\n\n"
            f"Ваш тестовый период заканчивается. Для продолжения работы необходимо выбрать тариф:\n\n"
            f"💰 <b>Вариант 1: Фикс за заявку</b>\n"
            f"• Пополняете депозит (от {MIN_DEPOSIT}₽)\n"
            f"• С каждого показанного телефона списывается {PRICE_PER_REQUEST}₽\n"
            f"• Платите только за реальные контакты\n\n"
            f"💳 <b>Вариант 2: Подписка</b>\n"
            f"• {SUBSCRIPTION_PRICE}₽/мес\n"
            f"• Безлимитные заявки\n\n"
            f"Для выбора тарифа свяжитесь с @ArTPart42admin"
        )
        await message.answer(f"✅ Уведомление отправлено {shop['name']}")
    except Exception as e:
        logger.error(f"Не удалось отправить: {e}")
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['transactions'])
async def show_transactions(message: types.Message):
    """История транзакций"""
    if message.chat.id != ADMIN_CHAT_ID:
        return
    
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /transactions <chat_id>")
        return
    
    try:
        chat_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    db = get_db()
    transactions = list(db['transactions'].find(
        {'shop_id': shop['_id']}
    ).sort('created_at', -1).limit(20))
    
    if not transactions:
        await message.answer(f"📭 У {shop['name']} нет транзакций")
        return
    
    text = f"💳 <b>История операций — {shop['name']}</b>\n\n"
    for tx in transactions:
        emoji = "💰" if tx['amount'] > 0 else "💸"
        text += f"{emoji} {tx['created_at'][:16]}\n"
        text += f"   {tx['description']}\n"
        text += f"   Сумма: {tx['amount']}₽ | Баланс: {tx['balance_after']}₽\n\n"
    
    await message.answer(text)


# === ФУНКЦИЯ ОТПРАВКИ ЗАЯВОК (вызывается из HTTP API) ===

async def send_request_to_shops(request_data):
    """Отправляет заявку магазинам"""
    db = get_db()
    
    # Сохраняем заявку
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
    
    # Получаем все активные магазины
    shops = list(db['shops'].find({'is_active': 1}))
    
    sent_count = 0
    for shop in shops:
        can_receive = False
        
        if shop['monetization_type'] == 'test':
            can_receive = True
        elif shop['monetization_type'] == 'subscription':
            if shop.get('subscription_end'):
                try:
                    end_date = datetime.fromisoformat(shop['subscription_end'])
                    if end_date > datetime.now():
                        can_receive = True
                except:
                    pass
        elif shop['monetization_type'] == 'fixed':
            if shop.get('deposit_balance', 0) >= PRICE_PER_REQUEST:
                can_receive = True
        
        if can_receive:
            # Создаём запись о рассылке
            db['shop_requests'].insert_one({
                'request_id': request_id,
                'shop_id': shop['_id'],
                'status': 'pending',
                'phone_shown': 0,
                'created_at': datetime.now().isoformat()
            })
            
            try:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(
                    InlineKeyboardButton("📞 Показать телефон", callback_data=f"show_phone_{request_id}"),
                    InlineKeyboardButton("❌ Нет в наличии", callback_data=f"reject_{request_id}")
                )
                
                text = (
                    f"🚗 <b>Новая заявка #{request_id}</b>\n\n"
                    f"🚙 Авто: {request_data.get('car_brand')} {request_data.get('car_model')}\n"
                )
                if request_data.get('vin'):
                    text += f"🔑 VIN: {request_data.get('vin')}\n"
                text += f"🔧 Запчасть: {request_data.get('part_name')}\n"
                if request_data.get('client_name'):
                    text += f"👤 Клиент: {request_data.get('client_name')}\n"
                
                text += "\nНажмите «📞 Показать телефон» чтобы связаться с клиентом"
                
                await bot.send_message(shop['chat_id'], text, reply_markup=keyboard)
                sent_count += 1
            except Exception as e:
                logger.error(f"Не удалось отправить магазину {shop['chat_id']}: {e}")
    
    logger.info(f"Заявка #{request_id} отправлена в {sent_count} магазинов")
    return str(request_id), sent_count

# === FASTAPI ПРИЛОЖЕНИЕ ===

# Проверка API ключа
async def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return x_api_key

@asynccontextmanager
async def lifespan(app: FastAPI):
    # При старте
    logger.info("🚀 Запуск ArTPart42 Shop Bot")
    
    # Проверяем подключение к MongoDB
    if test_connection():
        logger.info("✅ Подключение к MongoDB установлено")
    else:
        logger.warning("⚠️ Не удалось подключиться к MongoDB")
    
    # Инициализируем БД
    init_db()
    
    # Запускаем бота в фоне
    import asyncio
    asyncio.create_task(executor.start_polling(dp, skip_updates=True))
    
    yield
    
    # При остановке
    logger.info("🛑 Остановка бота")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    """Health check для Render"""
    return {"status": "ok", "service": "ArTPart42 Shop Bot"}

@app.get("/health")
async def health():
    """Детальная проверка здоровья"""
    db_ok = test_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/api/request")
async def create_request(request_data: dict, api_key: str = Depends(verify_api_key)):
    """Создание заявки (вызывается из приложения)"""
    try:
        request_id, sent_count = await send_request_to_shops(request_data)
        return {
            "success": True,
            "request_id": request_id,
            "shops_notified": sent_count
        }
    except Exception as e:
        logger.error(f"Ошибка создания заявки: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats")
async def get_stats(api_key: str = Depends(verify_api_key)):
    """Статистика (для админки)"""
    db = get_db()
    shops_count = db['shops'].count_documents({})
    requests_count = db['requests'].count_documents({})
    phone_shown = db['shop_requests'].count_documents({'phone_shown': 1})
    
    return {
        "shops": shops_count,
        "requests": requests_count,
        "phone_shown": phone_shown
    }

# === ЗАПУСК ===

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
