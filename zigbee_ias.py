from dataclasses import dataclass
from enum import IntFlag, IntEnum
import struct
from typing import Dict, Any, Union
import logging

#logging.basicConfig(
#    level=logging.WARNING,
#    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
#)
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.WARNING)  # Only logs from this module at INFO level will appear
#logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
class IASZoneStatus(IntFlag):
    ALARM1 = 0x0001
    ALARM2 = 0x0002
    TAMPER = 0x0004
    BATTERY = 0x0008
    SUPERVISION_REPORTS = 0x0010
    RESTORE_REPORTS = 0x0020
    TROUBLE = 0x0040
    AC_MAINS = 0x0080
    BATTERY_ERROR = 0x0200
    IASZ_STATE_NOT_ENROLLED = 0x00
    IASZ_STATE_ENROLLED=0x01

class MessageType(IntEnum):
    IAS_ZONE = 0x09
    BATTERY = 0x02
    CONFIG = 0x03
    STD_ZCL = 0x18

@dataclass
class IASZoneMessage:
    zone_status: IASZoneStatus
    extended_status: int
    zone_id: int
    delay: int
    
    @property
    def is_alarm_active(self) -> bool:
        return bool(self.zone_status & IASZoneStatus.ALARM1)
    
    @property
    def is_battery_low(self) -> bool:
        return bool(self.zone_status & IASZoneStatus.BATTERY)
    
    @property
    def is_tampered(self) -> bool:
        return bool(self.zone_status & IASZoneStatus.TAMPER)
    
    def get_state(self) -> dict:
        return {
            'contact': not self.is_alarm_active,
            'battery_low': self.is_battery_low,
            'tampered': self.is_tampered,
            'zone_id': self.zone_id,
            'raw_zone_status': self.zone_status.value
        }

@dataclass
class BatteryStatus:
    voltage: float
    percentage: int
    
    def get_state(self) -> dict:
        return {
            'battery': self.percentage,
            'voltage': round(self.voltage, 2),
            'battery_low': self.percentage < 20
        }

@dataclass
class ConfigStatus:
    reporting_config: Dict[str, Any]
    
    def get_state(self) -> dict:
        return {
            'type': 'configuration',
            'reporting': self.reporting_config
        }

class TS0203Parser:
    """Parser for TS0203 door sensor messages"""
    
    @staticmethod
    def parse_message(data: Union[bytes, str]) -> dict:
        """
        Parse a message from TS0203 sensor
        
        Args:
            data: Message data either as bytes or hex string
            
        Returns:
            Dictionary containing parsed message data
        """
        try:
            # Convert hex string to bytes if necessary
            if isinstance(data, str):
                message = bytes.fromhex(data)
            else:
                message = data
            
            # Determine message type from first byte
            LOGGER.info(f"message received : {message.hex()}")
            msg_type = message[0]
            
            if msg_type == MessageType.IAS_ZONE:
                return TS0203Parser._parse_ias_zone(message)
            elif msg_type == MessageType.BATTERY:
                return TS0203Parser._parse_battery(message)
            elif msg_type == MessageType.CONFIG:
                return TS0203Parser._parse_config(message)
            #elif msg_type == MessageType.STD_ZCL:
                #return TS0203Parser._parse_std_zcl(message)
            else:

                return {
                    'error': f'Unknown message type: 0x{msg_type:02x}',
                    'raw_message': data
                }
                
        except Exception as e:
            return {
                'error': f'Failed to parse message: {str(e)}',
                'raw_message': data
            }
    
    @staticmethod
    def _parse_ias_zone(message: bytes) -> dict:
        """Parse IAS Zone message"""
        zone_status, extended_status, zone_id, delay = struct.unpack('<HBHH', message[1:8])
        parsed = IASZoneMessage(
            zone_status=IASZoneStatus(zone_status),
            extended_status=extended_status,
            zone_id=zone_id,
            delay=delay
        )
        return parsed.get_state()
    
    @staticmethod
    def _parse_battery(message: bytes) -> dict:
        """Parse battery status message"""
        voltage_raw = struct.unpack('<H', message[2:4])[0]
        voltage = voltage_raw / 100.0
        percentage = message[5]
        
        parsed = BatteryStatus(voltage=voltage, percentage=percentage)
        return parsed.get_state()
    
    @staticmethod
    def _parse_config(message: bytes) -> dict:
        """Parse configuration/reporting message"""
        # Example configuration message parsing
        # 0300c33e200104010204010401000300000500000803000400050006000800001019000a00
        
        config = {
            'attributes': []
        }
        
        # Skip header (first 3 bytes)
        idx = 3
        while idx < len(message):
            try:
                # Each attribute report configuration contains:
                # - Attribute ID (2 bytes)
                # - Data Type (1 byte)
                # - Minimum reporting interval (2 bytes)
                # - Maximum reporting interval (2 bytes)
                # - Reportable change (variable based on data type)
                
                attr_id = struct.unpack('<H', message[idx:idx+2])[0]
                data_type = message[idx+2]
                min_interval = struct.unpack('<H', message[idx+3:idx+5])[0]
                max_interval = struct.unpack('<H', message[idx+5:idx+7])[0]
                
                attr_config = {
                    'attribute_id': f'0x{attr_id:04x}',
                    'data_type': f'0x{data_type:02x}',
                    'min_interval': min_interval,
                    'max_interval': max_interval
                }
                
                config['attributes'].append(attr_config)
                
                # Move to next attribute (assuming fixed 7-byte chunks)
                idx += 7
                
            except struct.error:
                break
        
        parsed = ConfigStatus(reporting_config=config)
        return parsed.get_state()
    @staticmethod
    def _parse_std_zcl(message: bytes) -> dict:
        # no definition found yet, simply return
        seq_numb, att_id, att_value, delay = struct.unpack('<BBHH', message[1:7])
        return 

# Create new parser for controller messages
class ControllerParser:
    def parse_message(self, message):
        """
        Parse controller messages and return structured data
        """
        try:
            # Add your specific message parsing logic here
            # This is an example structure - modify based on your actual messages
            result = {
                'type': 'controller_status',
                'status': True if 'running' in str(message).lower() else False,
                'message': str(message)
            }
            return result
        except Exception as e:
            LOGGER.error(f"Error parsing controller message: {str(e)}")
            return None

# Example usage
if __name__ == "__main__":
    # Test messages
    messages = [
        b'\t\x1a\x00\x00\x00\x00\x17\x00\x00',  # IAS Zone message
        "0200c33e0101",                          # Battery message
        "0300c33e200104010204010401000300000500000803000400050006000800001019000a00"  # Config message
    ]
    
    parser = TS0203Parser()
    
    print("Testing parser with different message types:\n")
    for msg in messages:
        result = parser.parse_message(msg)
        print(f"Message: {msg}")
        print("Parsed result:", result)
        print()