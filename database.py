"""
MongoDB database module for ArTPart42 Shop Bot
"""
import os
from pymongo import MongoClient
from pymongo.errors import CollectionInvalid

MONGODB_URL = os.getenv('MONGODB_URL')

client = None
db = None

def get_client():
    global client
    if client is None:
        client = MongoClient(MONGODB_URL)
    return client

def get_db():
    global db
    if db is None:
        db = get_client().get_default_database() if MONGODB_URL and '/' in MONGODB_URL.split('//')[1] else get_client()['artpart']
    return db

def test_connection():
    try:
        client = get_client()
        client.admin.command('ping')
        return True
    except Exception as e:
        print(f"MongoDB connection error: {e}")
        return False

def init_db():
    """Создаёт коллекции если их нет"""
    db = get_db()
    existing = db.list_collection_names()
    
    collections = ['shops', 'requests', 'shop_requests', 'transactions']
    for name in collections:
        if name not in existing:
            try:
                db.create_collection(name)
                print(f"✅ Коллекция {name} создана")
            except CollectionInvalid:
                pass
    
    print("✅ База данных инициализирована")

def execute_query(collection, query, data=None, insert=False, update=False):
    """Выполняет запрос к БД"""
    db = get_db()
    coll = db[collection]
    
    if insert:
        return coll.insert_one(data).inserted_id
    elif update:
        return coll.update_one(query, {'$set': data})
    else:
        return list(coll.find(query))

def execute_query_one(collection, query):
    """Возвращает один документ"""
    db = get_db()
    return db[collection].find_one(query)
