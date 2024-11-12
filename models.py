from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession 
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime
import logging
from contextlib import asynccontextmanager

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)  # Only logs from this module at INFO level will appear

# Database configuration
DATABASE_URL = "sqlite+aiosqlite:///zigbee_devices.db"
Base = declarative_base()

class DatabaseConnection:
    _instance = None
    _engine = None
    _async_session_maker = None

    @classmethod
    async def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance.initialize()
        return cls._instance

    async def initialize(self):
        if self._engine is None:
            self._engine = create_async_engine(
                DATABASE_URL,
                echo=False, # set to true will fill the consol with log msg
                connect_args={"check_same_thread": False}
            )
            self._async_session_maker = sessionmaker(
                self._engine,
                class_=AsyncSession,
                expire_on_commit=False
            )

    @asynccontextmanager
    async def get_session(self):
        if self._async_session_maker is None:
            await self.initialize()
        
        async with self._async_session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    async def init_db(self):
        if self._engine is None:
            await self.initialize()
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

# Model definitions
class Controller(Base):
    __tablename__ = 'controllers'
    
    id = Column(Integer, primary_key=True)
    ieee = Column(String, unique=True)
    status = Column(Boolean, default=False)
    last_seen = Column(DateTime)
    events = relationship("ControllerEvent", back_populates="controller")

class ControllerEvent(Base):
    __tablename__ = 'controller_events'
    
    id = Column(Integer, primary_key=True)
    controller_id = Column(Integer, ForeignKey('controllers.id'))
    event_type = Column(String)
    description = Column(String)
    timestamp = Column(DateTime, default=datetime.now)
    controller = relationship("Controller", back_populates="events")

class Device(Base):
    __tablename__ = 'devices'
    
    id = Column(Integer, primary_key=True)
    ieee = Column(String, unique=True, nullable=False)
    friendly_name = Column(String)
    device_type = Column(String)
    manufacturer = Column(String)
    model = Column(String)
    trigger = Column(Boolean, default=False) # field use to trigger camera recording
    mention = Column(Boolean, default=False) # in case of more door sensor use to get 
    # message or not
    created_at = Column(DateTime, default=datetime.now)
    last_seen = Column(DateTime)
    events = relationship("DeviceEvent", back_populates="device")

class DeviceEvent(Base):
    __tablename__ = 'device_events'
    
    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey('devices.id'))
    event_type = Column(String)
    description = Column(String)
    timestamp = Column(DateTime, default=datetime.now)
    device = relationship("Device", back_populates="events")

# Database interface for common operations
class DatabaseManager:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    @classmethod
    async def get_instance(cls):
        db_connection = await DatabaseConnection.get_instance()
        return cls(db_connection)

    async def get_device_by_ieee(self, ieee: str) -> Device:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Device).where(Device.ieee == str(ieee))
            )
            return result.scalar_one_or_none()

    async def add_device(self, device_data: dict) -> Device:
        async with self.db.get_session() as session:
            device = Device(**device_data)
            session.add(device)
            await session.commit()
            await session.refresh(device)
            return device
    async def remove_device(self, ieee: str) -> Device:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Device).where(Device.ieee == str(ieee))
            )
            device = result.scalar_one_or_none()
            await session.delete(device)
            await session.commit()
            
            return device
        
    async def add_device_event(self, device_id: int, event_type: str, description: str) -> DeviceEvent:
        async with self.db.get_session() as session:
            event = DeviceEvent(
                device_id=device_id,
                event_type=event_type,
                description=description
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

    async def update_device(self, ieee: str, update_data: dict) -> Device:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Device).where(Device.ieee == str(ieee))
            )
            device = result.scalar_one_or_none()
            if device:
                for key, value in update_data.items():
                    setattr(device, key, value)
                await session.commit()
                await session.refresh(device)
            return device
    
    async def get_controller(self, ieee: str) -> Controller:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Controller).where(Controller.ieee == str(ieee))
            )
            return result.scalar_one_or_none()
        
    async def add_controller(self, controller_data: dict) -> Controller:
        async with self.db.get_session() as session:
            controller = Controller(**controller_data)
            session.add(controller)
            await session.commit()
            await session.refresh(controller)
            return controller
        
    async def update_controller(self, controller) -> Controller:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(Controller).where(Controller.ieee==str(controller.ieee))
            )
            device = result.scalar_one_or_none()
            if device:
                device.status = controller.status
                device.last_seen = controller.last_seen
                await session.commit()
                await session.refresh(device)
            return device
        
    async def add_controller_event(self, controller_id: int, event_type: str, description: str) -> ControllerEvent:
        async with self.db.get_session() as session:
            event = ControllerEvent(
                device_id=controller_id,
                event_type=event_type,
                description=description
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

# Convenience function for initializing the database
async def init_zigbee_db():
    db_connection = await DatabaseConnection.get_instance()
    await db_connection.init_db()