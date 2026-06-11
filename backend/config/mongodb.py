from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from config.settings import Config
import logging

logger = logging.getLogger(__name__)

class MongoDB:
    _instance = None
    _client = None
    _db = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MongoDB, cls).__new__(cls)
        return cls._instance
    
    def connect(self):
        """Initialize MongoDB connection"""
        try:
            self._client = MongoClient(Config.MONGO_URL, serverSelectionTimeoutMS=5000)
            self._client.admin.command('ping')
            self._db = self._client[Config.DB_NAME]
            logger.info(f"Connected to MongoDB: {Config.DB_NAME}")
            self._create_collections()
            return self._db
        except ConnectionFailure as e:
            logger.error(f"MongoDB connection failed: {e}")
            raise
    
    def _create_collections(self):
        """Create necessary collections if they don't exist"""
        collections = [
            'candidates',
            'interviews',
            'interview_sessions',
            'evaluations',
            'interview_live_sessions'
        ]
        existing = self._db.list_collection_names()
        for collection in collections:
            if collection not in existing:
                self._db.create_collection(collection)
                logger.info(f"Created collection: {collection}")
    
    def get_db(self):
        """Get database instance"""
        if self._db is None:
            return self.connect()
        return self._db
    
    def close(self):
        """Close MongoDB connection"""
        if self._client:
            self._client.close()
            logger.info("MongoDB connection closed")

mongodb = MongoDB()
