#!/usr/bin/env python3

import asyncio
import aiofiles.os as aios
from quart import Quart, websocket, render_template_string,render_template,send_from_directory
from quart import request, redirect, url_for, flash,jsonify,send_from_directory,send_file
#from quart import BackgroundTask

from sqlalchemy import select,delete

from picamera2 import Picamera2,MappedArray
from picamera2.encoders import MJPEGEncoder,H264Encoder,Quality
from picamera2.outputs import FileOutput,CircularOutput
from libcamera import Transform,controls
from queue import Queue
from threading import Thread

import io
import cv2

import numpy as np
import time
from datetime import datetime
import psutil
import json
import os
import logging
import socket

# import from local definitions
from config import init_db, get_db, DatabaseOperations, Contact,Recording
from config import dongle, connection_status_processor
from config import wifi_con,connection_status_wifi,get_hotspot_status
from functools import wraps
# import for async functions
import threading
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
# import for pir detection
import RPi.GPIO as GPIO
# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

HOTSPOT_UUID = "fa1185cf-e8a2-4ad2-9b0e-bc1704f71b36"

app = Quart(__name__)
app.secret_key = 'your-secret-key'  # needed for flash messages
# add the test for 4G connection
app.context_processor(connection_status_processor)
app.context_processor(connection_status_wifi)

# Global variable to store the Monica instance for multi access
Monica = None
#processing_face_det = 'Yunet'  # Initial processing mode for facedetect
processing_state = True  # Initial processing state
min_area = 200 # minimum area for knn detection
# value is based on frame of 320*240= 76,800 pixels
# full body is about 53*80 pixels = 4240 pixels ( area)
# partial body detection is lower than 4240, so on the average 750

# To hold the current state of the sliders
slider_state = {
    'motion': 5,          # Default to "No Detection"
    'face_detect': 3,         # Default to "No Detection"
    'frameRatio': 2    # Default to "Frame Ratio 1"
}
face_detect_mapping = {
    0: 'Yunet',
    1: 'no processing',
    2: 'ssd',
    3: 'hog'
}
motion_detect_mapping = {
    0: 'mog',
    1: 'diff',
    2: 'numpy',
    3: 'no processing',
    4: 'blurred',
    5: 'knn'
}
connected_clients = []


preroll = 4 # preroll time of video recording prior of detection



# method to incrust text into GPU before encoding
# to be done adding frame around face detection
colour = (123, 255, 0) # light gray
origin = (0, 30)
font = cv2.FONT_HERSHEY_SIMPLEX
scale = 1
thickness = 2
cpul= 0

# Global variable to track the number of active WebSocket connections
active_connections = 0

#---------------------------------------------------------------------
#
# JSON config file
#
# config file used to define platform, camera and processing settings
# 
#--------------------------------------------------------------------

def load_config(config_file):
    
    try:
        with open(config_file, 'r') as f:
            print(f)
            return json.load(f)
    except Exception as e:
            logger.error(f"Error while opening config file: {config_file} with : {str(e)}")
            return None

def get_platform_settings(config):
    try:

        platform = config['basic_settings']['platform']
        return config['platform_settings'][platform]
    except Exception as e:
        logger.error(f"Error while reading platform settings : {str(e)}")
        return None


def get_camera_settings(config):
    camera_model = config['basic_settings']['camera']
    return config['camera_settings'][camera_model]

def get_motion_settings(config):
    motion_algo = config['basic_settings']['motion']
    return config['motion_settings'][motion_algo]

#----------------------------------------------------------------------
#
#   I/O BUFFER
#
#----------------------------------------------------------------------
# io buffer used for streaming
class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.queue = Queue()
        self.condition = threading.Condition()


    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

class AdaptiveClient:
    ''' this class is not used 
     not tested so far, the goal is to reduce frame quality
     and fps based on the load 
     to be investigated if needed''' 
    def __init__(self, websocket):
        self.websocket = websocket
        self.frame_rate = 25
        self.quality = 80
        self.last_latency = 0
        self.resolution = (640, 480)
        self.last_frame_time = time.time()

    async def adjust_quality(self, latency):
        if latency > 100:  # ms
            self.quality = max(30, self.quality - 10)
            self.frame_rate = max(10, self.frame_rate - 5)
        elif latency < 50:  # ms
            self.quality = min(80, self.quality + 5)
            self.frame_rate = min(25, self.frame_rate + 2)
#---------------------------------------------------------------------
#
#   VIDEO PROCESSOR
#
#---------------------------------------------------------------------

frame_queue = Queue()
detections_queue = asyncio.Queue(10)

class VideoProcessor:
    def __init__(self,app_loop,recording_dir='recordings',config_file="/home/baddemanax/python/tiny_doorhunter/config.json"):

        self.app_loop = app_loop
        self.motion_detected = 0
        self._shutdown = threading.Event()
        # queue for framee multiprocessing 
        self.detections = asyncio.Queue()
        self.recording_queue = Queue()
        self.recording_thread = threading.Thread(target=self.recording_worker, daemon=True)
        self.recording_thread.start()
        

        self.face_detected = 0
        self.recording = False
        self.video_writer = None
        
        
        self.ltime = None
        
        
        
        self.running = True
        # load json config file
        config= load_config(config_file)
        # get platform type ( pi4, Pi5, piW2..)
        platform_settings = get_platform_settings(config)
        camera_settings = get_camera_settings(config)
        self.recording_dir = recording_dir
		# Get the current directory where the script is located
        self.current_directory = os.path.dirname(__file__)
        self.filename = None
        # Define the relative path to the subfolder
        self.relative_path = os.path.join(self.current_directory, self.recording_dir)


        # debug purpose
        self.fps = 0
        self.cpu_load=0
        self.prev_time=0
        # Ensure the directory exists, or create it
        os.makedirs(self.relative_path, exist_ok=True)
        self.picam2 = Picamera2()
        # for pi camera wide angle no ir mode 1 2304x1296 [56.03 fps - (0, 0)/4608x2592 crop]
        # mode 2 has mode pixel but only 14fps 4608x2592 [14.35 fps - (0, 0)/4608x2592 crop]
        #
        mode = camera_settings['sensor_mode']
        mode=self.picam2.sensor_modes[mode]
        # load camera and gpu config from config.json
        framerate = config['basic_settings']['framerate']
        self.process_every_n_frames = config['basic_settings']['every_n_frames']
        slider_state['frameRatio']= self.process_every_n_frames
        self.decimation_factor = config['basic_settings']['decimation_factor']
        self.lores_size = (
            platform_settings['lores_resolution']['width'],
            platform_settings['lores_resolution']['height']
            )
        self.main_size = (
            platform_settings['main_resolution']['width'],
            platform_settings['main_resolution']['height']
        )
        format_main = platform_settings['main']
        format_lores = platform_settings['lores']
        self.inference = platform_settings['inference']
        vfl = camera_settings['vflip']
        hfl = camera_settings['hflip']

        video_config = self.picam2.create_video_configuration(
            sensor={"output_size":mode['size'],'bit_depth':mode['bit_depth']},
            main={"size": self.main_size, "format": format_main},
            lores={"size": self.lores_size, "format": format_lores},
            controls={'FrameRate': framerate, "FrameDurationLimits": (40000, 40000) ,'Saturation':0.0},
            transform=Transform(vflip=vfl,hflip=hfl)
        )
        self.picam2.configure(video_config)
        self.picam2.set_controls({
        "NoiseReductionMode": controls.draft.NoiseReductionModeEnum.HighQuality,
        "FrameDurationLimits": (40000, 40000),  # Lock to 12.5fps
        "AwbEnable": 0,  # Disable AWB for grayscale
        "AeEnable": 1,  # Keep AE enabled for proper exposure
    })
        self.angle = 0
        self.output = StreamingOutput()
        
        # doing the overlay after processing is better to avoid impacting detection
        self.picam2.post_callback = self.apply_timestamp
        # setting gpu encoder
        self.encoder = H264Encoder() # by default quality is set to medium
        # use circular output to have a preroll of 1 second
        self.circ=CircularOutput(buffersize=framerate*preroll)
        # write encoder output to circ buffer for recording
        
        self.picam2.start_encoder(self.encoder,self.circ,name="main")
        # start streaming mjpeg for web app
        self.picam2.start_recording(MJPEGEncoder(), FileOutput(self.output), name="lores")
        logger.info(f"camera initialised")
        
        
        self.face_detect_threshold = 0.80 # level above considered as good detection
        #self.face_detected = 0
        
        self.frame_count = 0
        
        

        # Parameters for motion detection
        
        
        self.motion_maxval = 255 # max value for frame comparison
        # drawback when using gpu incrust, it plays a role in the motion detection, as the 
        # incrusted frame is compared to previous one
        
        # value above is clipped to motion_maxval this help detection
        self.knn_threshold = 180
        self.knn = cv2.createBackgroundSubtractorKNN(
                   history=300,
                   dist2Threshold=400, # default is 400 ( distance )
                   detectShadows=False
                ) 
        # to reduce processing we decimate original lores size by decimation_factor
        self.lores_half_size = (self.lores_size[0]//self.decimation_factor,self.lores_size[1]//self.decimation_factor)
        
        
        # Performance settings
        
        self.resolution = self.lores_half_size
        # mapping to use webslider, this should be removed for final application
        self.toggle_facedetect = face_detect_mapping[slider_state['face_detect']]
        self.toggle_motion = motion_detect_mapping[slider_state['motion']]
        # for knn rectangle detection
        self.box = (0,0,0,0)
        # parameter for the recording worker as it is now run in a separated thread
        self.recording_state = {
            'is_recording': False,
            'start_time': None,
            'last_detection_time': None,
            'filename': None
        }
        self.recording_config = {
            'max_duration': 30,  # maximum recording duration in seconds
            'cooldown_time': 5   # time without detections before stopping
        }
        self.thumbnail = None # picture derived from video clip
        self.thumbnail_flag = False # without good person algo we should keep 
        # thumbnail processing to only 1 shot ( cpu constraint)

        # get zerotier ip address
        interfaces = psutil.net_if_addrs()
        zerotier_ip = None
        for interface, addr_info in interfaces.items():
            if 'zt' in interface:  # Look for ZeroTier interface
                for addr in addr_info:
                    if addr.family == socket.AF_INET:  # Check for IPv4 address
                        zerotier_ip = addr.address
                        break
        self.ip = zerotier_ip
        
        logger.info(f" zerotier ip address {self.ip}")

    #- - - - - - - - - - - 
    # ROTATE
    #- - - - - - - - - - -   
    def rotate(self,angle):
        self.angle= angle
    #- - - - - - - - - - -
    # TIMESTAMP and DETECTIONBOX
    #- - - - - - - - - - -
    def apply_timestamp(self,request):
        ''' we run the processing in the "mapped array" this avoid copying buffers'''
        timestamp = time.strftime("%Y-%m-%d %X")
        
        
        with MappedArray(request, "lores") as m:
            curr_time = time.time()
            self.frame_count+=1
            # process every other frame
            if self.frame_count % self.process_every_n_frames == 0:
                # flag use only to deactivate knn
                if self.toggle_motion == 'knn':
                    gray_frame = cv2.resize(m.array, self.lores_half_size, interpolation=cv2.INTER_AREA)
                    self.box=self.motion_detect_knn(gray_frame)
                    # in the mean time and for ios thumbnail
                    if self.motion_detected==1 and self.thumbnail_flag:
                         logging.info(f"motion detected creating thumbnail {self.motion_detected}")
                         _, jpeg_bytes = cv2.imencode('.jpg', gray_frame)
                         self.thumbnail = jpeg_bytes
                         self.thumbnail_flag=False
                    

            
            # add time stamp after motion detection and boxes around 
            cv2.putText(m.array, timestamp, origin, font, scale, colour, thickness)
            
            if self.box is not None:
                
                (x,y,w,h) = self.box
                (x, y, w, h) = [i * self.decimation_factor for i in (x, y, w, h)]
                cv2.rectangle(m.array, (x, y), (x+w, y+h), (0, 255, 0), 2)
            # should be removed if in production
            if curr_time - self.prev_time >= 1:
                    self.fps = self.frame_count / (curr_time - self.prev_time)
                    self.frame_count = 0
                    self.prev_time = curr_time
                    
                    # Calculate CPU load
                    self.cpu_load = psutil.cpu_percent()
            
            
    def grayout(self,request):
        # can be used as pre buffer to convert color to gray
        # this prevents main output to be gray
        # must be in YUV420 we just keep the Y plane
        with MappedArray(request, "lores") as m:
            
            # Extract Y (luminance) plane as grayscale
            m.array[:self.lores_size[1], :self.lores_size[0]] = m.array[:self.lores_size[1], :self.lores_size[0]]

            # Zero out U and V planes (half resolution for YUV420)
            uv_height = self.lores_size[1] // 2
            uv_width = self.lores_size[0] // 2
        
            # U plane
            m.array[self.lores_size[1]:self.lores_size[1] + uv_height, :uv_width] = 0
            # V plane
            m.array[self.lores_size[1] + uv_height:self.lores_size[1] + 2 * uv_height, :uv_width] = 0

    #- - - - - - - - - - - - - - -
    # MOTION DETECTION KNN
    #- - - - - - - - - - - - - - -
    def motion_detect_knn(self,frame):
            
            tm= cv2.TickMeter()
            tm.start()
            self.motion_detected = 0
            fg_mask = self.knn.apply(frame)
            # Apply threshold to the mask
            
            _, thresh = cv2.threshold(fg_mask, self.knn_threshold, self.motion_maxval, cv2.THRESH_BINARY)
            # Find contours
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            self.motion_detected =0
            for contour in contours:
                if cv2.contourArea(contour) > min_area:
                    self.motion_detected =1
                    asyncio.run_coroutine_threadsafe(self.detections.put(('camera', time.time())), self.app_loop)
                   
            images = None
            if len(contours)>=1 & self.motion_detected ==1:
                # Combine all contours into one array
                all_points = np.vstack(contours)
                
                # Find bounding box for all points
                x, y, w, h = cv2.boundingRect(all_points)
                
                
                    
                images=(x,y,w,h)       

            tm.stop()
            #print(f"time for knn detection {round(tm.getTimeMilli(),2)} ms")
            tm.reset()
            return images
    
    def handle_recording(self):
        
        if self.face_detected:
            if not self.recording:
                self.recording = True
                # Get current date and time
                current_time = datetime.now()

                # Format the timestamp as YYMMDD_HHMM
                formatted_time = current_time.strftime("%y%m%d_%H%M%S")
                print(f"time {formatted_time}")
                self.filename = os.path.join(self.relative_path, f"detection_{formatted_time}")
                self.circ.fileoutput=self.filename
                self.circ.start()
                
            self.ltime = time.time()  # Set ltime when recording starts and continue until
            

        if self.recording and self.ltime is not None and time.time() - self.ltime > 8:
            self.recording = False
            #camera.stop_encoder()
            self.circ.stop()
            self.video_writer = None
            self.ltime = None  # Reset ltime when recording stops
            print("Saving file",self.filename)
            self.face_detected=0 # reset flag in case of processing every other frame
            print()
            print("Preparing an mp4 version")
            # convert file to mp4
            cmd = 'ffmpeg -nostats -loglevel 0 -r 30 -i ' + self.filename + ' -c copy ' + self.filename +'.mp4'
            os.system(cmd)
            # delete tmp file
            cmd ='rm ' + self.filename 
            os.system(cmd)
            # we need to await an async function
            #self.handle_sms(self.filename+".mp4")
            # if we are not in an async context we need a synchronous wrapper
            #result = self.sync_handle_sms(f"{self.filename}.mp4")
            asyncio.run_coroutine_threadsafe(
                        self.handle_sms(f"{self.filename}.mp4"), 
                        self.app_loop
                    ).result()
            asyncio.run_coroutine_threadsafe(
                        self.save_recording_metadata(f"{self.filename}.mp4"), 
                        self.app_loop
                    ).result()
            print("Removing h264 version")
            print("Waiting for next trigger")
    
    def recording_worker(self):
        ''' thread that is supposed to handle recording upon detection
        goal is to keep recording while several detection but to stop after max sec
        if no more detection keep runing for only y sec'''
        while True:
            # queue used to comunnicate from main thread
            start_time = self.recording_queue.get()
            if start_time:
                logger.info(f" start time received {start_time}")
                self.start_recording()

            while True:
                try:
                    current_time = time.time()
                    # if recording reaches max duration stop
                    if current_time - start_time > self.recording_config['max_duration']:
                        self.stop_recording()
                        break
                    # if there are new detection before y sec keep going
                    elif self.recording_queue.qsize() !=0:
                        start_time = self.recording_queue.get()
                        logger.info(f" new start time received {start_time}")
                    # if no more detection only records for x sec    
                    elif self.recording_queue.qsize() == 0 and current_time - start_time > self.recording_config['cooldown_time']:
                        self.stop_recording()
                        break
                    else:
                        time.sleep(1)
                except Exception as e:
                    logger.error(f"Error in recording_worker: {str(e)}")
                    break
       
    def start_recording(self):
        """Start a new recording if not already recording"""
        logger.info(f"start recording mp4")
        if not self.recording_state['is_recording']:
            # reset thumbnail flag such it takes second detection... sligthly better 
            self.thumbnail_flag = True
            self.recording_state['is_recording'] = True
            self.recording_state['start_time'] = time.time()
            
            # Get current date and time for filename
            current_time = datetime.now()
            formatted_time = current_time.strftime("%y%m%d_%H%M%S")
            self.recording_state['filename'] = os.path.join(self.relative_path, f"detection_{formatted_time}")
            
            # Start the circular buffer output
            self.circ.fileoutput = self.recording_state['filename']
            self.circ.start()
            
            logger.info(f"Started recording: {self.recording_state['filename']}")

    def stop_recording(self):
        """Stop the current recording and process the file"""
        if self.recording_state['is_recording']:
            self.recording_state['is_recording'] = False
            self.circ.stop()
            
            filename = self.recording_state['filename']
            logger.info(f"Stopped recording: {filename}")
            
            # Convert to MP4
            mp4_filename = f"{filename}.mp4"
            cmd = f'ffmpeg -nostats -loglevel 0 -r 30 -i {filename} -c copy {mp4_filename}'
            os.system(cmd)
            
            # Clean up original file
            os.system(f'rm {filename}')
            
            # Send notifications and save metadata
            # not in async def
            asyncio.run_coroutine_threadsafe(
                        self.handle_sms(f"{filename}.mp4"), 
                        self.app_loop
                    ).result()
            asyncio.run_coroutine_threadsafe(
                        self.save_recording_metadata(f"{filename}.mp4"), 
                        self.app_loop
                    ).result()
            # Reset recording state
            self.recording_state['start_time'] = None
            self.recording_state['last_detection_time'] = None
            self.recording_state['filename'] = None


    async def handle_sms(self, filename):
        # to be done sending the url instead filename
        url = f"http://{self.ip}:8000/get_recording{filename}"
        msg = f"Motion Detected check the file: {url}"
        logger.info(f"sms msg : {msg}")
        try:
            async for db in get_db():
                success_count, total_contacts = await DatabaseOperations.send_sms_to_contacts(db, msg)
                logger.info(f"SMS sent successfully: {success_count}/{total_contacts}")
                return success_count, total_contacts
        except Exception as e:
            logger.error(f"Error sending SMS: {str(e)}")
            return 0, 0
    async def save_recording_metadata(self,filename):
        try:
            async for db in get_db():
                success = await DatabaseOperations.add_recording(db,filename.lstrip('/'),thumbnail=self.thumbnail)
                logger.info(f"success in writing recording metadata {success}")
        except Exception as e:
            logger.error(f"Error while writing recording metadata {e}")

    def shutdown(self):
        self._shutdown.set()
    
    async def alert_generator(self):
        recent_detections = []
        logger.info(f"init alert generator")
        while True:
            try:
                logger.info("alert_generator waiting for detection")
                # get all detections from whateever modules ( PIR, zigbee, camera)
                detection = await self.detections.get()
                #logger.info(f"alert_generator received detection: {detection}")
                # append to a list for further procesising
                recent_detections.append(detection)
            
                current_time = time.time()
                # if multiple detections occures in less than X seconds, we consider it the same event
                recent_detections = [d for d in recent_detections if current_time - d[1] < 5]
                #logger.info(f"recents detections {recent_detections}")
                # a real detection should give more than 2 different events in X seconds ( PIR and CAMERA ...)
                if len(set(d[0] for d in recent_detections)) >= 2:
                    alert_message = json.dumps({"type": "alert", "message": "Multiple detections occurred!"})
                    logger.info(f"multiple detections occur")
                    # send signal to recording thread
                    self.recording_queue.put(current_time)
                if detection[0]=='camera':
                    self.recording_queue.put(detection[1])
                    
            except Exception as e:
                    logger.error(f"Error in alert_generator: {str(e)}")
                    await asyncio.sleep(1)  # Prevent tight loop in case of errors

"""
async def broadcast( message):
        for client in self.clients.values():
            await client.websocket.send(message)
"""
#------------------------------------------------------------------------------
#
#   INDEX
#
#------------------------------------------------------------------------------
@app.route('/')
async def index():
    status = await dongle.get_connection_status()
    #wifi_status = await wifi_con.get_active_connection_by_uuid(HOTSPOT_UUID)
    #print(wifi_status)
    #return await render_template('index.html', connection_status=status)
    return await render_template('index.html')

# for android phone, they point to index.html
@app.route('/index.html')
async def index_index():
    
    return await render_template('index.html')

#------------------------------------------------------------------------------
#
#   STATUS
#
#------------------------------------------------------------------------------


@app.route('/status')
async def status():
    
    return await render_template('status.html')

#------------------------------------------------------------------------------
#
#   SETTINGS
#
#------------------------------------------------------------------------------

@app.route('/settings')
async def settings():
    
    return await render_template('settings.html')

@app.route('/rotate_camera', methods=['POST'])
async def rotate_camera():
    form = await request.form
    angle = form.get('angle',0) 
    Monica.rotate(int(angle))
    return redirect(url_for('settings'))
#**********************
#
#   WIFI
#
#**********************
def error_handler(f):
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    return decorated_function

@app.route('/status_wifi')
@error_handler
async def status_wifi():
    is_active =  get_hotspot_status()
    
    return jsonify({'status': 'active' if is_active else 'inactive'})

@app.route('/toggle_wifi/<action>')
@error_handler
async def toggle_wifi(action):
    if action not in ['on', 'off']:
        return jsonify({'error': 'Invalid action'}), 400
    
    connection = wifi_con.get_connection_by_uuid(HOTSPOT_UUID)
    if not connection:
        return jsonify({'error': f'Connection with UUID {HOTSPOT_UUID} not found'}), 404

    if action == 'on':
        # Activate the hotspot connection
        wifi_con.set_active_connection(connection)
        message = "Hotspot turned on"
    else:
        # Deactivate the hotspot connection
        active_conn = wifi_con.get_active_connection_by_uuid(HOTSPOT_UUID)
        if active_conn:
            active_conn.Delete()
        message = "Hotspot turned off"
    
    return jsonify({'success': True, 'message': message})

@app.websocket('/ws_pictrl')
async def ws_pictrl():
     while True:
        data = await websocket.receive_json()
        logger.info(f"received socket msg : {data}")
        if data['action'] == 'reboot':
            if data['confirm']:
                await websocket.send('Rebooting...')
                #logger.info("receive socket reboot")
                asyncio.create_task(reboot())
            else:
                await websocket.send('Reboot cancelled')
        elif data['action'] == 'shutdown':
            if data['confirm']:
                await websocket.send('Shutting down...')
                #logger.info("received socket shutdown")
                asyncio.create_task(shutdown())
            else:
                await websocket.send('Shutdown cancelled')

async def reboot():
    
    logger.info(f"system is rebooting from webapp")
    os.system('sudo /sbin/reboot')

async def shutdown():
    logger.info(f"system is shutting down from webapp ")
    
    os.system('sudo /sbin/shutdown -h now')


#--------------------------------------------------------------
#
#       CONTACT
#
#--------------------------------------------------------------

@app.route('/contact')
async def contact():
    contacts = []
    async for db in get_db():
        result = await db.execute(select(Contact))
        contacts = result.scalars().all()
    
    return await render_template('contact.html', contacts=contacts)

# Route to display the Add Contact page
@app.route('/add_contact')
async def add_contact():
    return await render_template('add_contact.html')
@app.route('/create_contact', methods=['POST'])

async def create_contact():
    form = await request.form
    contact_data = {
        'alias': form['alias'],
        'first_name': form['first_name'],
        'last_name': form['last_name'],
        'email': form['email'],
        'phone': form['phone']
    }

    async for db in get_db():
        try:
            await DatabaseOperations.add_contact(db, contact_data)
            await flash('Contact added successfully!', 'success')
        except Exception as e:
            await flash(f'Error adding contact: {str(e)}', 'error')
    
    return redirect(url_for('contact'))

@app.route('/update_contact/<int:id>', methods=['POST'])
async def update_contact(id):
    form = await request.form
    async for db in get_db():
        contact = await DatabaseOperations.get_contact_by_id(db, id)
        if contact:
            mail_send = 'mail_send' in form
            sms_send = 'sms_send' in form
            await DatabaseOperations.update_contact(db, contact, mail_send=mail_send, sms_send=sms_send)
    return redirect(url_for('contact'))

@app.route('/update_contact_checkbox', methods=['POST'])
async def update_contact_checkbox():
    form = await request.form
    contact_id = int(form.get('id'))
    field = form.get('field')
    value = form.get('value')

    async for db in get_db():
        contact = await DatabaseOperations.get_contact_by_id(db, contact_id)
        if contact:
            update_data = {
                field: True if value == '1' else False
            }
            await DatabaseOperations.update_contact(db, contact, **update_data)

    return redirect(url_for('contact'))

@app.route('/edit_contact/<int:id>', methods=['GET', 'POST'])
async def edit_contact(id):
    async for db in get_db():
        contact = await DatabaseOperations.get_contact_by_id(db, id)
        if not contact:
            await flash('Contact not found', 'error')
            return redirect(url_for('contact'))

        if request.method == 'POST':
            form = await request.form
            update_data = {
                'first_name': form.get('first_name'),
                'last_name': form.get('last_name'),
                'email': form.get('email'),
                'phone': form.get('phone')
            }
            await DatabaseOperations.update_contact(db, contact, **update_data)
            return redirect(url_for('contact'))

        return await render_template('edit_contact.html', contact=contact)


@app.route('/delete_contact/<int:id>', methods=['POST'])
async def delete_contact(id):
    async for db in get_db():
        contact = await DatabaseOperations.get_contact_by_id(db, id)
        if contact:
            try:
                await DatabaseOperations.delete_contact(db, contact)
                await flash('Contact deleted successfully!', 'success')
            except Exception as e:
                await flash(f'Error deleting contact: {str(e)}', 'error')
    return redirect(url_for('contact'))




#---------------------------------------------------------------------
#
#     WEBSOCKET
#
#---------------------------------------------------------------------
@app.websocket('/ws')
async def ws():
    global active_connections,Monica
    active_connections += 1
    
    try:
        while True:
        
            with Monica.output.condition:
                Monica.output.condition.wait()
                frame = Monica.output.frame
                await websocket.send(frame)
           

    finally:
        print('DEBUG : one connexion less')
        active_connections -= 1

@app.websocket('/debug_data')
async def debug_data():
    global Monica
    while True:
        # Calculate frame rate
        fps = round(Monica.fps)
        
        # Calculate CPU load
        cpu_load = Monica.cpu_load

        # Get motion detection and face detection results
        
        motion_detected = bool(Monica.motion_detected) if isinstance(Monica.motion_detected, np.bool_) else Monica.motion_detected
        face_detected = bool(Monica.face_detected) if isinstance(Monica.face_detected, np.bool_) else Monica.face_detected
        
        # Send debug data to browser
        debug_data = {
            'fps': fps,
            'cpu_load': cpu_load,
            'motion_detected': motion_detected,
            'face_detected': face_detected
        }
        await websocket.send(json.dumps(debug_data))

        # Wait for a short period before sending the next debug data
        await asyncio.sleep(0.1)

@app.websocket('/toggle_facedetect')
async def toggle_facedetect():
    global Monica
    global connected_clients, processing_face_det,processing_state,face_detect_mapping
    # Register the new client
    connected_clients.append(websocket._get_current_object())
    # Send the current slider state to the newly connected client
    await websocket.send(json.dumps(slider_state))
    try:
        
        # Listen for incoming messages (mode changes)
        while True:
            
            
            data = await websocket.receive()
            
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                print("Invalid JSON received")
                continue

            # Update the server's slider state based on the received message
            # Update server-side slider state based on the received message
            if message.get('type') == 'motion':
                mode_value = int(message.get('mode'))
                # Use the mapping to convert the integer to the corresponding mode
                Monica.toggle_motion = motion_detect_mapping.get(mode_value, 'no processing')
                print(f"toggle motion {Monica.toggle_motion}")
                slider_state['motion'] = message.get('mode')
            elif message.get('type') == 'face_detect':
                mode_value = int(message.get('mode'))
                # Use the mapping to convert the integer to the corresponding mode
                Monica.toggle_facedetect = face_detect_mapping.get(mode_value, 'no processing')
                print(f"toggle face detect {Monica.toggle_facedetect}")
                slider_state['face_detect'] = message.get('mode')
            elif message.get('type') == 'frameRatio':
                print(f"Received WebSocket message: {message}")
                mode_value = int(message.get('value'))
                Monica.process_every_n_frames=mode_value
                print(f"process every {Monica.process_every_n_frames} frame")
                slider_state['frameRatio'] = message.get('value')
            
            
                # Broadcast the mode and state change to all clients
            for client in connected_clients:
                try:
                    await client.send(json.dumps(slider_state))
                    
                except Exception as e:
                    print(f"Failed to send message to client: {e}")
    except Exception as e:
        print(f"WebSocket error: {e}")

    finally:
        # Remove the client from the connected list if the connection is closed
        connected_clients.remove(websocket._get_current_object())
#------------------------------------------------------------------
#
#       HISTORY
#
#------------------------------------------------------------------
# Route to serve the watch recordings page

@app.route('/history')
async def history():
    async for db in get_db():
    
        recordings = await DatabaseOperations.get_recordings(db)
        #print(recordings)
    return await render_template('db_history.html', recordings=recordings)

@app.route('/recordings/<filename>')
async def recordings(filename):
    logger.info(f"recording files name to be played {filename}")
    recordings_path = os.path.join(app.root_path, 'recordings')
    return await send_from_directory(recordings_path, filename)

async def serve_recording(filename):
    #await asyncio.sleep(0.1)  # Delay to reduce conflict with live stream
    await send_file(f"/{filename}", conditional=True)

@app.route('/get_recording/<path:filename>')
async def get_recording(filename):
    logger.info(f"recording file to be played {filename}")
    #task = app.add_background_task(serve_recording, filename)
    #await task.start() 
    #return "Playback initiated.", 200
    return await send_file(f"/{filename}",conditional=True)

@app.route('/get_thumbnail/<int:recording_id>')
async def get_thumbnail(recording_id):
    logger.info(f"request for thumbnail")
    try:
        async for db in get_db():
            recording = await DatabaseOperations.get_recording_id(db,recording_id)
            if recording.thumbnail:
                return await send_file(
                    io.BytesIO(recording.thumbnail),
                    mimetype='image/jpeg',
                    as_attachment=False
                )
        else:
            return 'Thumbnail not found', 404
    except Exception as e:
        logger.error(f"error while reading thumbnail from db {str(e)}")
    

@app.route('/recordings/<int:recording_id>/toggle-reminder', methods=['POST'])

async def toggle_reminder(recording_id):
    logger.info(f"toggle reminder {recording_id}")
    data = await request.get_json()
    reminder = data['reminder']
    try:
        async for db in get_db():
            recording = await DatabaseOperations.get_recording_id(db,recording_id)
            recording.reminder = reminder
    
            await db.commit()
            return jsonify({'message': 'Reminder toggled successfully'})

        return redirect(url_for('history'))  
    except Exception as e:
        logger.error(f'error while trying to change reminder in db {str(e)}')
    
@app.route('/delete_recording/<int:recording_id>', methods=['POST'])
async def delete_recording(recording_id):
    async for db in get_db():
        recording = await DatabaseOperations.get_recording_id(db,recording_id)
        if not recording:
                await flash('Recording not found', 'error')
                return  redirect(url_for('history'))
        try:
            # Store filename before deletion
            filename = recording.filename
            # Delete from database
            stmt = delete(Recording).where(Recording.id == recording_id)
            await db.execute(stmt)
            await db.commit()
        
            recording_path = f"/{recording.filename}"
            if await aios.path.exists(recording_path):
                await aios.remove(recording_path)
            else:
                logger.error(f"not a valid path {recording_path}")
            
            await flash('Recording deleted successfully!', 'success')
        except Exception as e:
            await db.rollback()
            await flash(f'Error deleting recording: {e}', 'danger')
    return redirect(url_for('history'))
"""
@app.route('/history')
async def history():
    # Path to the recordings directory
    recordings_path = os.path.join(app.root_path, 'recordings')
    
    # Get a list of all MP4 files in the recordings directory
    recordings = [f for f in os.listdir(recordings_path) if f.endswith('.mp4')]
    
    # Render the history.html template and pass the list of recordings
    return await render_template('history.html', recordings=recordings)

# Serve individual MP4 files from the 'static/recordings' directory
@app.route('/static/recordings/<filename>')
async def serve_recording(filename):
    #print(f"{filename}")
    recordings_path = os.path.join(app.root_path, 'recordings')
    return await send_from_directory(recordings_path, filename)
"""
#---------------------------------------------------------------------
#
# 4G PROCESSING
#
#---------------------------------------------------------------------

# Add a route to manually send SMS
@app.route('/send_sms/<phone_number>/<message>')
async def send_sms(phone_number, message):
    success = await dongle.send_sms(phone_number, message)
    if success:
        await flash('SMS sent successfully!', 'success')
    else:
        await flash('Failed to send SMS', 'error')
    return redirect(url_for('index'))
#---------------------------------------------------------------
#
#       VERSION
#
#---------------------------------------------------------------
@app.route('/version')
async def version():
    
    return await render_template('version.html')

#---------------------------------------------------------------
#
#               BEFORE SERVING
#
# the server and the process need to start before a client connects
# we init the database
# we start the thread videoprocessor
#
#---------------------------------------------------------------

@app.before_serving
async def startup():
    global Monica
    logger.info("Starting up application...")
    await init_db()
    # Store the main event loop
    main_loop = asyncio.get_event_loop()
    logger.info('starting video processor')
    Monica = VideoProcessor(main_loop)
    
    #Thread(target=Monica.camera_thread, daemon=True).start()
    logger.info(f"starting alert generator")
    try:
        asyncio.create_task(Monica.alert_generator())
    except Exception as e:
        logger.error(f" error while launching alert generator {e}")
    # Test connection status at startup
    status = await dongle.get_connection_status()
    logger.info(f"Initial 4G connection status: {'Connected' if status else 'Disconnected'}")

@app.after_serving
async def shutdown():
    Monica.shutdown()
    

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8000)