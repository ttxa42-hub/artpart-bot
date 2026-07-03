"""
Модуль работы с MongoDB Atlas
"""
import os
from pymongo import MongoClient
from datetime import datetime

def get_db():
    """Возвращает подключение к MongoDB"""
    url = os.getenv('MONGODB_URL')
    if not url:
        raise ValueError("MONGODB_URL не установлена")
    client = MongoClient(url)
    return client['artpart42']

def init_db():
    """Инициализирует базу данных (создаёт коллекции)"""
    db = get_db()
    # В MongoDB коллекции создаются автоматически при первой вставке
    # Но мы можем создать их явно
    db.create_collection('shops')
    db.create_collection('requests')
    db.create_collection('shop_requests')
    db.create_collection('transactions')
    print("✅ База данных инициализирована")

def execute_query(collection_name, query, params=None, fetch=False, insert=False):
    """Выполняет запрос к MongoDB"""
    db = get_db()
    collection = db[collection_name]
    
    if insert:
        result = collection.insert_one(params)
        return result.inserted_id
    
    if fetch:
        results = list(collection.find(query, params))
        return results
    
    # Для update/delete
    if params and '$set' in params:
        collection.update_one(query, params)
    else:
        collection.update_one(query, {'$set': params} if params else {})
    
    return None

def execute_query_one(collection_name, query, params=None):
    """Выполняет запрос и возвращает одну запись"""
    db = get_db()
    collection = db[collection_name]
    result = collection.find_one(query, params)
    return result

def test_connection():
    """Проверяет подключение к БД"""
    try:
        db = get_db()
        db.client.admin.command('ping')
        return True
    except Exception as e:
        print(f"❌ Ошибка подключения к БД: {e}")
        return False
