import asyncio
import logging
from datetime import datetime
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from zigpy_deconz.zigbee.application import ControllerApplication
# for raspbee zigbee dongle
from zigpy_deconz.zigbee.application import ControllerApplication as raspbee_ctrlApp
# for EZ zigbee dongle
from bellows.zigbee.application import ControllerApplication as bellowz_ctrlApp
import zigpy.config as conf
from models import Device, DeviceEvent,Controller,ControllerEvent,DatabaseManager
#from models import get_db
from zigbee_ias import TS0203Parser,ControllerParser
import serial.tools.list_ports
#from enum import Enum
#from typing import Callable, Dict, List

#logging.basicConfig(
#    level=logging.WARNING,
#    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
#)
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)  # Only logs from this module at INFO level will appear
#logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
parser = TS0203Parser()



# controller for raspbee II or EZSP
# it is meant to initialise the right driver based on the VID:PIB by manufacturer
class ZigbeeController:
    # Known USB VID:PID pairs for our dongles
    RASPBEE_IDS = [
        ("1cf1", "0030")  # Common RaspBee VID:PID
    ]
    
    EZSP_IDS = [
        ("10c4", "8a2a"),  # Silicon Labs EZSP
        ("1a86", "7523")   # Alternative EZSP ID
    ]
    SONOFF = [("1a86","55d4") ]
    def __init__(self,app_loop, notification_callback=None):
        # connect to small database to store zigbee event and devices 
        # so far i couldn't make it run in async, so.....
        self.engine = create_engine('sqlite:///zigbee_devices.db')
        Session = sessionmaker(bind=self.engine)
        self.session = Session()
        self.app_loop= app_loop
        # async link with database, i guess code should be cleaned if in use
        self._db_manager = None
        self.app = None
        self.notification_callback = notification_callback
        # in order to add more callbacks
        self.new_device_callbacks = []
        # tested on 2 dongles raspbee and ezsp
        self.detected_dongle = None
        self.port = None
        # class to analyse coordinator message
        # the goal is to provide user a visual feedback of coordinator status
        self.controller_parser = ControllerParser()
        self.controller_id = None
        #self.Process=Process(self)

    async def initialize(self):
        # Get the database manager instance for async connection
        # which is defined in models.py
        # as this didn't work in callback, maybe best to keep only sync connection ?
        self._db_manager = await DatabaseManager.get_instance()
        #self.event_logger = EventLogger(self._db_manager)
        #self.device_manager = DeviceManager(self._db_manager)

    def detect_dongle(self):
        """Detect which Zigbee dongle is connected."""
        ports = list(serial.tools.list_ports.comports())
        
        for port in ports:
            # Extract VID and PID from the port hardware ID
            vid_pid = self._extract_vid_pid(port.hwid)
            if not vid_pid:
                continue
                
            vid, pid = vid_pid
            
            if (vid.lower(), pid.lower()) in self.RASPBEE_IDS:
                self.detected_dongle = "raspbee"
                self.port = port.device
                LOGGER.info(f"RaspBee detected on {port.device}")
                return "raspbee"
                
            elif (vid.lower(), pid.lower()) in self.EZSP_IDS:
                self.detected_dongle = "ezsp"
                self.port = port.device
                LOGGER.info(f"EZSP detected on {port.device}")
                return "ezsp"
            elif (vid.lower(), pid.lower()) in self.SONOFF:
                self.detected_dongle = "sonoff"
                self.port = port.device
                LOGGER.info(f"sonoff detected on {port.device}")
                return "ezsp"
        try:
            # if no other dongle we try raspbee, not yet found a good way to detect
            # serial hat :(
            self.detected_dongle = "raspbee"
            self.port = '/dev/ttyS0'
            LOGGER.info(f"RaspBee detected on {self.port}")
            return "raspbee"
        except Exception as e:
            LOGGER.error(f"couldn't connect to raspbee serial {str(e)}")

        
        LOGGER.error("No compatible Zigbee dongle detected")
        return None
    
    def _extract_vid_pid(self, hwid):
        """Extract VID and PID from hardware ID string."""
        try:
            # Hardware ID format usually contains VID/PID like "USB VID:PID=1234:5678"
            vid_pid = hwid.split("VID:PID=")[1].split()[0].split(":")
            return vid_pid[0], vid_pid[1]
        except (IndexError, AttributeError):
            return None  
        
    async def initialize_dongle(self,port='/dev/ttyS0'):
        """Initialize the detected dongle with appropriate settings."""
        if not self.detected_dongle:
            self.detect_dongle()
            
        if self.detected_dongle == "raspbee":
            # add return state to avoid error
            ret = await self._init_raspbee(port=port)
            if not ret:
                return 0
        
        elif self.detected_dongle == "ezsp":
            await self._init_ezsp()
        else:
            raise RuntimeError("No compatible dongle detected")
        # Get controller information after initialization
        controller_info = await self._get_controller_info()
        LOGGER.info(f"coordinator : {controller_info}")
        if controller_info:
            # Store controller in database
            #async with await self._db_manager.get_session() as session:
            #async for session in get_db():
            controller = await self._db_manager.get_controller(str(controller_info['ieee']))
                #result = await session.execute(select(Controller).where(Controller.ieee == controller_info['ieee']))
                #controller = result.scalar_one_or_none()
                #controller = session.query(Controller).filter_by(
                #    ieee=controller_info['ieee']
                #    ).first()
            
            if not controller:
                    controller = {
                        'ieee' : controller_info['ieee'],
                        
                        #model=controller_info['model'],
                        #manufacturer=controller_info['manufacturer'],
                        'status' : True,
                        'last_seen':datetime.now()
                    }
                    #session.add(controller)
                    controller = await self._db_manager.add_controller(controller)
            else:
                    controller.status = True
                    controller.last_seen = datetime.now()
                    
                    #controller.model = controller_info['model']
                    #controller.manufacturer = controller_info['manufacturer']
            
                    #await session.commit()
                    await self._db_manager.update_controller(controller)
                    self.controller_id = controller.id
            
                # Log controller startup
            self. _log_controller_event(
                    controller.id,
                    'startup', 
                    f'Controller {controller_info["model"]} initialized '
                )
            
            # Notify status callback
            for callback in self.new_device_callbacks:
               if callback.__name__ =='zigbee_dongle_status':
                   asyncio.create_task(callback(controller_info))
               #if self.status_callback:
               #     await self.status_callback(True)
            return 1
        return 0

          
    def add_new_device_callback(self, callback):
        LOGGER.info(f"adding new callback {callback.__name__}")
        self.new_device_callbacks.append(callback) 

    async def _init_raspbee(self,port='/dev/ttyS0'):
        """Initialize RaspBee dongle.
        Values are so far hardcoded, should be passed to better fit diff configuration
        on PI 4 we only need to add in config.txt : enable_uart=1
        while on piw2 we also need to add in config.txt : dtoverlay=disable-bt
        and to suppress in  cmdline.txt : console=serial0,115200"""

        try:
            from zigpy_deconz.exception import CommandError, ParsingError, MismatchedResponseError
            
            radio_config = {
            conf.CONF_DEVICE: {
                conf.CONF_DEVICE_PATH: port,
                conf.CONF_DEVICE_BAUDRATE: 38400,
            },
            "database_path": "zigbee.db",
            "network": {
                "channel": 15,
                "pan_id": None,
                "extended_pan_id": None,
            },
            "connect": {
                "timeout": 60,
                "attempts": 3,
            }
            }
            LOGGER.info('create zigbee controller app')
            for attempt in range(radio_config["connect"]["attempts"]):
                try:
                    LOGGER.info(f"Connection to raspbee attempt {attempt + 1}")
                    self.app = await raspbee_ctrlApp.new(
                        radio_config
                        #auto_form=True, # this doesn't work on piW
                        #start_radio=True # cannot start radio as such on piw2 while on pi4
                        )
                    break
                #except (CommandError, MismatchedResponseError, ParsingError) as e:
                except Exception as e:
                    if attempt < radio_config["connect"]["attempts"] - 1:
                        await asyncio.sleep(5)
                    else:
                        LOGGER.error(f"Failed to connect 2 RaspBee: {str(e)}")
                        return 0
            
            # callback listener
            listener = ZigbeeListener(self)
            self.app.add_listener(listener)
            await self.app.startup(auto_form=True)
            
            LOGGER.info("RaspBee initialized successfully")
            return 1
        except Exception as e:
            LOGGER.error(f"Failed to initialize RaspBee: {str(e)}")
            return 0
    
    async def _init_ezsp(self):
        """Initialize EZSP dongle."""
        try:
            config = {
            conf.CONF_DEVICE: {
                conf.CONF_DEVICE_PATH: self.port,
                conf.CONF_DEVICE_BAUDRATE: 115200,
                },  
            'network': {
                'key': [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31],  # Example network key
                'channels': [15]  # Select channel (Zigbee uses channels 11-26)
                }
            }
            self.app= await bellowz_ctrlApp.new(config)
            
            
            # callback listener
            listener = ZigbeeListener(self)
            self.app.add_listener(listener)
            LOGGER.info("EZSP initialized successfully")
        except Exception as e:
            LOGGER.error(f"Failed to initialize EZSP: {str(e)}")
            raise

    async def _get_controller_info(self):
        """Get controller information from the ZigBee application"""
        
        try:
            if self.app and hasattr(self.app, 'devices'):
                
                # Coordinator is always at network address 0x0000
                coordinator=self.app.get_device(nwk=0x0000)
                
                #LOGGER.info(f"corrdinator A : {coordinator}")
                coordinator_info = {
                    'ieee': str(coordinator.ieee),
                    'nwk': '0x0000',
                    #'pan_id': hex(self.app.state.network_info.pan_id) if self.app.state.network_info else None,
                    #'channel': self.app.state.network_info.channel if self.app.state.network_info else None,
                    'model': coordinator.model,
                    'manufacturer': coordinator.manufacturer
                }
                
                #LOGGER.info(f"coordinator  : {coordinator_info}")
                return coordinator_info
            return None
        
        except Exception as e:
            LOGGER.error(f"Failed to get controller info: {str(e)}")
            return None
    
    async def start_scanning(self, duration=60):
        # scan to add new zigbee sensors in case of
        if self.app:
            await self.app.permit(duration)
            self._log_event(None, 'status', f'Started scanning for devices for {duration} seconds')
    
    async def device_joined(self, device):
        # add new device to database
        # function never called, all done in callback
        LOGGER.info(f"Device joined: {device.ieee}")
        
        new_device = {
            'ieee': str(device.ieee),
            'device_type':'unknown',
            'manufacturer':device.manufacturer,
            'model':device.model,
            'last_seen':datetime.now()
            }
        await self._db_manager.add_device(new_device)
        self._log_event(new_device.id, 'status', 'Device joined network')
        # Notify registered callbacks about the new device
        """
        for callback in self.new_device_callbacks:
            if callback.__name__ =='zigbee_notify_new_device':
                asyncio.create_task(callback(new_device))
        """    
    async def remove_device(self, ieee):
        # remove device from database and network
        
        device = await self._db_manager.get_device_by_ieee(ieee)
        if device:
            # Remove from zigbee network
            if self.app and ieee in self.app.devices:
                await self.app.remove(ieee)
            
            # Remove from database
            self._log_event(device.id, 'status', f'Device removed: {device.friendly_name or ieee}')
            
            await self._db_manager.remove_device(ieee)
            
    async def update_friendly_name(self, ieee, friendly_name):
        # zigbee addresses not easy to read, so alias more convenient
        
            
        device = await self._db_manager.update_device(ieee,{'friendly_name': friendly_name})        
        self._log_event(device.id, 'status', f'Friendly name updated to: {friendly_name}')
    
    def _log_event(self, device_id, event_type, description):
        LOGGER.info(f"log event {device_id}")
        try:
            
            event = DeviceEvent(
                device_id=device_id,
                event_type=event_type,
                description=description
            )
            self.session.rollback()
            self.session.add(event)
            self.session.commit()
            
            
            #await self._db_manager.add_device_event(device_id, event_type, description)
 
        except Exception as e:
            LOGGER.error(f"error logging info {str(e)}")
    def _log_controller_event(self, controller_id,event_type, description):
        """Log controller-specific events"""
        try:
            
            if self.controller_id:
                event = ControllerEvent(
                    controller_id=self.controller_id,
                    event_type=event_type,
                    description=description
                )
                
                self.session.add(event)
                self.session.commit()
            
            #await self._db_manager.add_controller_event(controller_id,event_type, description)
        except Exception as e:
            LOGGER.error(f"Error logging controller event: {str(e)}")

#-----------------------------------------------------------------
#
# CALLBACK class for message from zigpy
#
#-----------------------------------------------------------------

# callback class
class ZigbeeListener:
    def __init__(self, controller):
        self.controller = controller
        self.session = controller.session

    def get_device(self,ieee):
        try:
            result = self.controller.session.query(Device).filter_by(ieee=ieee).first()
  
            
            return result
        except Exception as e:
            LOGGER.error(f"error while trying to get device {str(e)}")
            return None
    def device_joined(self, device):
        LOGGER.info(f"callback Device joined: {device.ieee}")
        
        try:
            
            new_device = Device(
            ieee=str(device.ieee),
            device_type='unknown',  # Will be updated when initialized
            manufacturer=device.manufacturer,
            model=device.model,
            last_seen=datetime.now()
            )
            self.controller.session.add(new_device)
            self.controller.session.commit()
            self.controller._log_event(new_device.id, 'status', 'Device joined network')
            for callback in self.controller.new_device_callbacks:
                if callback.__name__ =='zigbee_notify_new_device':
                    asyncio.create_task(callback(new_device))
            
        except Exception as e:
            LOGGER.error(f"error in callback device_joined {str(e)}")
        
    
    def device_initialized(self, device):
        """Handle device initialization and update device type based on clusters."""
        LOGGER.info(f"Callback Device initialized: {device.ieee}")
        try:
            db_device = self.controller.session.query(Device).filter_by(ieee=str(device.ieee)).first()
            #db_device = self.controller.device_manager.get_device_sync(ieee=str(device.ieee))
            if db_device:
                if hasattr(device, 'endpoints'):
                    LOGGER.info(f"Device has endpoints: {device.endpoints}")
                    for endpoint_id, ep in device.endpoints.items():
                        if endpoint_id == 0:  # Skip ZDO endpoint
                            continue
                        
                        LOGGER.info(f"Analyzing endpoint {endpoint_id}")
                        LOGGER.info(f"Profile: {ep.profile_id if hasattr(ep, 'profile_id') else 'Unknown'}")
                        LOGGER.info(f"Input clusters: {ep.in_clusters}")
                    
                        # Get the profile ID from the endpoint
                        try:
                            # For newer zigpy versions
                            profile = getattr(ep, 'profile_id', None)
                            if profile is None:
                                # For older zigpy versions
                                profile = getattr(ep, 'profile', None)
                            
                            LOGGER.info(f"Detected profile: {profile}")
                        
                            if profile == 260:  # Home Automation profile
                                # Get in_clusters dictionary
                                in_clusters = getattr(ep, 'in_clusters', {})
                            
                                # Update device type based on clusters
                                if 0x0006 in in_clusters:  # On/Off cluster
                                    db_device.device_type = 'door'
                                    LOGGER.info(f"Setting device type to 'door' for {device.ieee}")
                                elif 0x0406 in in_clusters:  # Occupancy sensing
                                    db_device.device_type = 'presence'
                                    LOGGER.info(f"Setting device type to 'presence' for {device.ieee}")
                                elif 0x0402 in in_clusters:  # Temperature measurement
                                    db_device.device_type = 'temperature'
                                    LOGGER.info(f"Setting device type to 'temperature' for {device.ieee}")
                                elif 0x0400 in in_clusters:  # Illuminance measurement
                                    db_device.device_type = 'illuminance'
                                    LOGGER.info(f"Setting device type to 'illuminance' for {device.ieee}")
                                elif 0x0101 in in_clusters:  # Door Lock
                                    db_device.device_type = 'lock'
                                    LOGGER.info(f"Setting device type to 'lock' for {device.ieee}")
                            
                                # Add more cluster checks as needed
                            
                        except AttributeError as e:
                            LOGGER.warning(f"Could not access profile for endpoint {endpoint_id}: {e}")
                        
                self.controller.session.commit()
                #self.controller.event_logger.log_event_sync(
                self.controller._log_event(
                    device_id=str(device.ieee),
                    event_type="status",
                    description=f"Device {device.ieee} initialized"
                    )
            
        except Exception as e:
            LOGGER.error(f"Error in device_initialized for {device.ieee}: {e}", exc_info=True)
            
    def attribute_updated(self, device, cluster, attribute_id, value):
        db_device = self.controller.session.query(Device).filter_by(ieee=str(device.ieee)).first()
        if db_device:
            description = f"Attribute updated: {cluster.name} - {attribute_id}: {value}"
            self.controller._log_event(db_device.id, 'action', description)
            
            # Handle door sensor events
            if db_device.device_type == 'door' and cluster.name == 'on_off':
                if self.controller.notification_callback:
                    asyncio.create_task(
                        self.controller.notification_callback(
                            db_device.friendly_name or str(device.ieee),
                            'Door ' + ('opened' if value else 'closed')
                        )
                    )
            
            # Handle presence sensor events
            elif db_device.device_type == 'presence' and cluster.name == 'occupancy':
                if self.controller.notification_callback:
                    asyncio.create_task(
                        self.controller.notification_callback(
                            db_device.friendly_name or str(device.ieee),
                            'Motion ' + ('detected' if value else 'cleared')
                        )
                    )
                    
    def device_left(self, device):
        LOGGER.info(f" Callback device {device.ieee} left")
        db_device = self.controller.session.query(Device).filter_by(ieee=str(device.ieee)).first()
                
        if db_device:
            self.controller._log_event(db_device.id, 'status', 'Device left network')
            
    
    def handle_message(self, device, profile, cluster, src_ep, dst_ep, message):
        """Called when any zigbee message is received."""
        # works
        LOGGER.info(
            f"Received message from {device.ieee}: "
            f"profile: {profile}, cluster: {cluster}, "
            f"src_ep: {src_ep}, dst_ep: {dst_ep}, "
            f"message: {message}"
        )
        db_device = self.controller.session.query(Device).filter_by(ieee=str(device.ieee)).first()
        #db_device = self.controller.device_manager.get_device_sync(ieee=str(device.ieee))
        
        if db_device:
            try:
                #LOGGER.info(f"db device {db_device}")
                # try to parse message
                result = parser.parse_message(message)
                #print(result)
                description = (
                    f"Attribute updated: {db_device.friendly_name or str(device.ieee)} - "
                    f"{'opened' if result['contact'] else 'closed'} -"
                    f" battery : {'low' if result['battery_low'] else 'ok'}")
                # self.controller._log_event(db_device.id, 'action', description)
                self.controller._log_event(
                    device_id=str(device.ieee),
                    event_type="action",
                    description=description
                    )
                LOGGER.info(f"descr {description}")
                # Handle door sensor events
                #if db_device.device_type == 'door' and cluster.name == 'on_off':
                if self.controller.notification_callback:
                        asyncio.create_task(
                            self.controller.notification_callback(
                                db_device.friendly_name or str(device.ieee),
                                'Contact ' + ('opened' if result['contact'] else 'closed')
                            )
                        )
                
            except Exception as e:
                LOGGER.error(f"error in writing to db {str(e)}")
        else:
            db_controller = self.controller.session.query(Controller).filter_by(ieee=str(device.ieee)).first()
            if db_controller:
                description = (
                    f"Controller updated: {str(device.ieee)} - "
                    f"no idea what it means -"
                    f" msg : {message.tohex()}")
                # self.controller._log_event(db_device.id, 'action', description)
                self.controller._log_controller_event(
                    device_id=str(device.ieee),
                    event_type="action",
                    description=description
                    )

            #LOGGER.info(f"device {device.ieee} not in db")
        