#!/usr/bin/env python3
"""
ArTPart42 Shop Bot - aiogram 3.x + FastAPI + MongoDB
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
import uvicorn

from database import init_db, execute_query, execute_query_one, test_connection, get_db
from bson import ObjectId

load_dotenv()

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
API_KEY = os.getenv('API_KEY', 'default-secret-key-change-me')
PORT = int(os.getenv('PORT', '8000'))
DEFAULT_TEST_DAYS = int(os.getenv('DEFAULT_TEST_DAYS', '30'))
PRICE_PER_REQUEST = int(os.getenv('PRICE_PER_REQUEST', '100'))
SUBSCRIPTION_PRICE = int(os.getenv('SUBSCRIPTION_PRICE', '3000'))
DEPOSIT_WARNING_THRESHOLD = int(os.getenv('DEPOSIT_WARNING_THRESHOLD', '300'))
MIN_DEPOSIT = int(os.getenv('MIN_DEPOSIT', '1000'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
storage = MemoryStorage()
router = Router()
dp = Dispatcher(storage=storage)
dp.include_router(router)

class ShopRegistration(StatesGroup):
    waiting_for_name = State()
    waiting_for_city = State()
    waiting_for_phone = State()
    waiting_for_email = State()

def log_transaction(shop_id, tx_type, amount, balance_after, description, request_id=None):
    execute_query('transactions', {}, {
        'shop_id': shop_id, 'type': tx_type, 'amount': amount,
        'balance_after': balance_after, 'description': description,
        'request_id': str(request_id) if request_id else None,
        'created_at': datetime.now().isoformat()
    }, insert=True)

def get_tariff_display(shop):
    if shop['monetization_type'] == 'test':
        return None
    elif shop['monetization_type'] == 'fixed':
        return f"💰 Фикс: {PRICE_PER_REQUEST}₽/заявка\n💳 Баланс: {shop['deposit_balance']}₽"
    elif shop['monetization_type'] == 'subscription':
        if shop.get('subscription_end'):
            try:
                end_date = datetime.fromisoformat(shop['subscription_end'])
                days_left = (end_date - datetime.now()).days
                if days_left > 0:
                    return f"💳 Подписка до {end_date.strftime('%d.%m.%Y')} ({days_left} дн.)"
            except: pass
        return f"💳 Подписка: {SUBSCRIPTION_PRICE}₽/мес"
    return None

def get_tariff_display_admin(shop):
    if shop['monetization_type'] == 'test':
        return " ТЕСТ"
    elif shop['monetization_type'] == 'fixed':
        return f" ФИКС | Депозит: {shop['deposit_balance']}₽"
    elif shop['monetization_type'] == 'subscription':
        return f"💳 ПОДПИСКА"
    return "❓ НЕ ВЫБРАН"

@router.message(Command("start"))
async def cmd_start(message: Message):
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
    await message.answer(
        "📖 <b>Как это работает:</b>\n\n"
        "1️⃣ Клиент оставляет заявку на сайте artpart42.ru\n"
        "2️ Заявка автоматически приходит вам в бот\n"
        "3️⃣ Вы нажимаете [📞 Показать телефон]\n"
        "4️⃣ Связываетесь с клиентом напрямую\n\n"
        "По вопросам: @ArTPart42admin"
    )

@router.message(Command("register"))
@router.message(F.text == "🏢 Регистрация магазина")
async def cmd_register(message: Message):
    shop = execute_query_one('shops', {'chat_id': message.chat.id})
    if shop:
        await message.answer(f"️ Магазин уже зарегистрирован:\n {shop['name']}")
        return
    
    await message.answer("🏢 <b>Регистрация магазина</b>\n\nВведите название:")
    await ShopRegistration.waiting_for_name.set()

@router.message(ShopRegistration.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("📍 Введите город:")
    await ShopRegistration.waiting_for_city.set()

@router.message(ShopRegistration.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    await state.update_data(city=message.text)
    await message.answer("📞 Введите телефон:")
    await ShopRegistration.waiting_for_phone.set()

@router.message(ShopRegistration.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("📧 Введите email:")
    await ShopRegistration.waiting_for_email.set()

@router.message(ShopRegistration.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    data = await state.get_data()
    test_end = datetime.now() + timedelta(days=DEFAULT_TEST_DAYS)
    
    execute_query('shops', {}, {
        'name': data['name'], 'city': data['city'],
        'chat_id': message.chat.id, 'phone': data['phone'],
        'email': message.text, 'is_active': 1,
        'monetization_type': 'test', 'deposit_balance': 0,
        'test_period_end': test_end.isoformat(),
        'created_at': datetime.now().isoformat()
    }, insert=True)
    
    await state.clear()
    await message.answer(
        f"✅ <b>Магазин зарегистрирован!</b>\n\n"
        f"🏢 {data['name']}\n {data['city']}\n"
        f"📞 {data['phone']}\n📧 {message.text}\n\n"
        f"Тестовый период до: {test_end.strftime('%d.%m.%Y')}"
    )

@router.message(F.text == "💳 Мой тариф")
@router.message(Command("tariff"))
async def my_tariff(message: Message):
    shop = execute_query_one('shops', {'chat_id': message.chat.id})
    if not shop:
        await message.answer("⚠️ Сначала зарегистрируйтесь: /register")
        return
    
    tariff_info = get_tariff_display(shop)
    if tariff_info is None:
        await message.answer(f"💳 <b>Тариф</b>\n\n🏢 {shop['name']}\n\nТариф не выбран.")
    else:
        await message.answer(f"💳 <b>Ваш тариф:</b>\n\n🏢 {shop['name']}\n\n{tariff_info}")

@router.message(F.text == "📋 Мои заявки")
async def my_requests(message: Message):
    shop = execute_query_one('shops', {'chat_id': message.chat.id})
    if not shop:
        await message.answer("️ Сначала зарегистрируйтесь: /register")
        return
    
    shop_requests = execute_query('shop_requests', {'shop_id': shop['_id']}, fetch=True)
    shop_requests = sorted(shop_requests, key=lambda x: x.get('created_at', ''), reverse=True)[:10]
    
    if not shop_requests:
        await message.answer("📭 У вас пока нет заявок")
        return
    
    text = "📋 <b>Последние 10 заявок:</b>\n\n"
    for sr in shop_requests:
        request = execute_query_one('requests', {'_id': sr['request_id']})
        if not request: continue
        
        status = "📞" if sr.get('phone_shown') else "" if sr.get('status') == 'rejected' else "⏳"
        text += f"{status} #{str(request['_id'])[-6:]} | {request['car_brand']} {request['car_model']}\n"
        text += f"   📝 {request['part_name']}\n\n"
    
    await message.answer(text)

@router.message(F.text == "📊 Статистика")
@router.message(Command("stats"))
async def stats(message: Message):
    shop = execute_query_one('shops', {'chat_id': message.chat.id})
    if not shop:
        await message.answer("⚠️ Сначала зарегистрируйтесь: /register")
        return
    
    all_requests = execute_query('shop_requests', {'shop_id': shop['_id']}, fetch=True)
    total = len(all_requests)
    phone_shown = len([r for r in all_requests if r.get('phone_shown')])
    
    text = f"📊 <b>Статистика:</b>\n\n🏢 {shop['name']}\n📥 Всего: {total}\n📞 Показано: {phone_shown}"
    tariff_info = get_tariff_display(shop)
    if tariff_info: text += f"\n\n{tariff_info}"
    
    await message.answer(text)

@router.callback_query(F.data.startswith('show_phone_'))
async def callback_show_phone(callback_query: CallbackQuery):
    request_id_str = callback_query.data.split('_', 2)[2]
    chat_id = callback_query.message.chat.id
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await callback_query.answer("❌ Магазин не найден", show_alert=True)
        return
    
    try:
        request_id = ObjectId(request_id_str)
    except:
        await callback_query.answer("❌ Неверный ID заявки", show_alert=True)
        return
    
    shop_request = execute_query_one('shop_requests', {'request_id': request_id, 'shop_id': shop['_id']})
    if not shop_request or shop_request.get('phone_shown'):
        await callback_query.answer("⚠️ Уже обработано", show_alert=True)
        return
    
    if shop['monetization_type'] == 'fixed':
        if shop['deposit_balance'] < PRICE_PER_REQUEST:
            await callback_query.answer(f"⚠️ Недостаточно средств: {shop['deposit_balance']}₽", show_alert=True)
            return
        
        new_balance = shop['deposit_balance'] - PRICE_PER_REQUEST
        db = get_db()
        db['shops'].update_one({'_id': shop['_id']}, {'$set': {'deposit_balance': new_balance}})
        log_transaction(shop['_id'], 'request_charge', -PRICE_PER_REQUEST, new_balance, f'Заявка #{str(request_id)[-6:]}', request_id)
        
        db['shop_requests'].update_one({'_id': shop_request['_id']}, {'$set': {'phone_shown': 1, 'status': 'responded'}})
    else:
        db = get_db()
        db['shop_requests'].update_one({'_id': shop_request['_id']}, {'$set': {'phone_shown': 1, 'status': 'responded'}})
    
    request = execute_query_one('requests', {'_id': request_id})
    await callback_query.message.edit_text(
        f"✅ <b>Заявка #{str(request_id)[-6:]}</b>\n\n"
        f"🚙 {request['car_brand']} {request['car_model']}\n"
        f"🔧 {request['part_name']}\n\n"
        f"📞 <b>{request['client_phone']}</b>\n"
        f"👤 {request.get('client_name', 'Не указан')}"
    )
    await callback_query.answer("✅ Готово")

@router.callback_query(F.data.startswith('reject_'))
async def callback_reject(callback_query: CallbackQuery):
    request_id_str = callback_query.data.split('_', 1)[1]
    chat_id = callback_query.message.chat.id
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop: return
    
    try:
        request_id = ObjectId(request_id_str)
    except:
        await callback_query.answer("❌ Неверный ID", show_alert=True)
        return
    
    db = get_db()
    db['shop_requests'].update_one({'request_id': request_id, 'shop_id': shop['_id']}, {'$set': {'status': 'rejected'}})
    
    await callback_query.message.edit_text(f" <b>Заявка #{str(request_id)[-6:]}</b>\n\nОтмечено: нет в наличии")
    await callback_query.answer("Отмечено")

@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.chat.id != ADMIN_CHAT_ID:
        await message.answer("⛔ Доступ запрещён")
        return
    
    db = get_db()
    shops_count = db['shops'].count_documents({})
    requests_count = db['requests'].count_documents({})
    
    await message.answer(
        f" <b>Админ-панель</b>\n\n"
        f"🏢 Магазинов: {shops_count}\n"
        f" Заявок: {requests_count}\n\n"
        f"Команды:\n"
        f"/shops - список\n"
        f"/shop_info <chat_id>\n"
        f"/add_deposit <chat_id> <amount>\n"
        f"/set_fixed <chat_id>\n"
        f"/set_subscription <chat_id> <months>\n"
        f"/set_test <chat_id> <days>"
    )

@router.message(Command("shops"))
async def list_shops(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    db = get_db()
    shops = list(db['shops'].find({'is_active': 1}))
    if not shops:
        await message.answer(" Магазинов нет")
        return
    text = "🏢 <b>Магазины:</b>\n\n"
    for shop in shops:
        text += f"{shop['name']} ({shop['city']})\nID: <code>{shop['chat_id']}</code>\n{get_tariff_display_admin(shop)}\n\n"
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
    except:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    new_balance = shop['deposit_balance'] + amount
    db = get_db()
    db['shops'].update_one({'_id': shop['_id']}, {'$set': {'deposit_balance': new_balance}})
    log_transaction(shop['_id'], 'deposit_topup', amount, new_balance, 'Пополнение')
    
    await message.answer(f"✅ Пополнено\n💳 Баланс: {new_balance}₽")

@router.message(Command("set_fixed"))
async def set_fixed(message: Message):
    if message.chat.id != ADMIN_CHAT_ID: return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /set_fixed <chat_id>")
        return
    try: chat_id = int(args[1])
    except:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    db = get_db()
    db['shops'].update_one({'_id': shop['_id']}, {'$set': {'monetization_type': 'fixed'}})
    await message.answer(f"✅ {shop['name']} → Фикс")

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
    except:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    sub_end = datetime.now() + timedelta(days=30 * months)
    db = get_db()
    db['shops'].update_one({'_id': shop['_id']}, {'$set': {'monetization_type': 'subscription', 'subscription_end': sub_end.isoformat()}})
    await message.answer(f"✅ Подписка до {sub_end.strftime('%d.%m.%Y')}")

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
    except:
        await message.answer("❌ Неверный формат")
        return
    
    shop = execute_query_one('shops', {'chat_id': chat_id})
    if not shop:
        await message.answer("❌ Магазин не найден")
        return
    
    test_end = datetime.now() + timedelta(days=days)
    db = get_db()
    db['shops'].update_one({'_id': shop['_id']}, {'$set': {'monetization_type': 'test', 'test_period_end': test_end.isoformat()}})
    await message.answer(f"✅ Тест до {test_end.strftime('%d.%m.%Y')}")

async def send_request_to_shops(request_data):
    db = get_db()
    result = db['requests'].insert_one({
        'vin': request_data.get('vin'), 'car_brand': request_data.get('car_brand'),
        'car_model': request_data.get('car_model'), 'part_name': request_data.get('part_name'),
        'client_name': request_data.get('client_name'), 'client_phone': request_data.get('client_phone'),
        'status': 'new', 'created_at': datetime.now().isoformat()
    })
    request_id = result.inserted_id
    
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
                'request_id': request_id, 'shop_id': shop['_id'],
                'status': 'pending', 'phone_shown': 0,
                'created_at': datetime.now().isoformat()
            })
            
            try:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📞 Показать телефон", callback_data=f"show_phone_{request_id}")],
                    [InlineKeyboardButton(text="❌ Нет в наличии", callback_data=f"reject_{request_id}")]
                ])
                
                text = f"🚗 <b>Заявка #{str(request_id)[-6:]}</b>\n\n"
                text += f" {request_data.get('car_brand')} {request_data.get('car_model')}\n"
                text += f"🔧 {request_data.get('part_name')}\n"
                if request_data.get('client_name'): text += f"👤 {request_data.get('client_name')}\n"
                
                await bot.send_message(shop['chat_id'], text, reply_markup=keyboard)
                sent_count += 1
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")
    
    return str(request_id), sent_count

async def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return x_api_key

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Запуск ArTPart42 Shop Bot")
    if test_connection():
        logger.info("✅ MongoDB подключена")
    init_db()
    poll_task = asyncio.create_task(dp.start_polling(bot))
    yield
    poll_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "ok", "service": "ArTPart42 Shop Bot"}

@app.get("/health")
async def health():
    return {"status": "ok" if test_connection() else "degraded"}

@app.post("/api/request")
async def create_request(request_data: dict, api_key: str = Depends(verify_api_key)):
    try:
        request_id, sent_count = await send_request_to_shops(request_data)
        return {"success": True, "request_id": request_id, "shops_notified": sent_count}
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
