import pycurl 
try: 
    from io import BytesIO 
except ImportError: 
    from StringIO import StringIO as BytesIO 
try: 
    from urllib.parse import urlencode 
except ImportError: 
    from urllib import urlencode 
import urllib3
import json 
import time 
import socket
import numpy as np
import cv2
import argparse
import time
from datetime import datetime, timedelta, timezone
from tzlocal import get_localzone
import os
import queue
import threading
import dpkt
from scapy.all import wrpcap, Ether, IP, UDP
from scapy.utils import PcapWriter

import logging
logger = logging.getLogger()
logger.setLevel('DEBUG')
BASIC_FORMAT = "%(asctime)s.%(msecs)03d:%(levelname)s:%(message)s"
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
formatter = logging.Formatter(BASIC_FORMAT, DATE_FORMAT)
terminalHandler = logging.StreamHandler() # 输出到控制台的handler
terminalHandler.setFormatter(formatter)
terminalHandler.setLevel('INFO')  # 也可以不设置，不设置就默认用logger的level
logger.addHandler(terminalHandler)

# velodyne
# https://github.com/valgur/velodyne_decoder
import velodyne_decoder as vd
from velodyne_decoder_pylib import *

exitFlag=False
class ld:
    """
    Class for velodyne lidars.
    """
    def __init__(self, model:str='',
                 lidarip:str='192.168.1.201', 
                 dataPort:int=2368, 
                 rpm:int=600,
                 retrunMode:str='dual',
                 localhost:str='',
                 as_pcl_structs:bool=False) -> None:
        # velodyne lidar
        self.lidarip=lidarip
        assert dataPort in range(65535),f"Arg 'dataPort' has to be in [0,65535], but got {dataPort}."
        self.dataPort=dataPort
        self.as_pcl_structs=as_pcl_structs
        self.model=model
        self.rpm=rpm
        assert retrunMode in ['strongest','last','dual'], f"returnMode must be one of ['strongest','last','dual'], but got {retrunMode}"
        self.retrunMode=retrunMode.capitalize()
        self.config=vd.Config(model=self.model, rpm=self.rpm)
        self.decoder = vd.StreamDecoder(self.config)
        
        # communication
        self.localhost=localhost
        self.sensor = pycurl.Curl()
        self.Base_URL = 'http://'+self.lidarip+'/cgi/'
        print(f"Base_URL:{self.Base_URL}")
        self.buffer = BytesIO()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((self.localhost, self.dataPort))
        
        # data container
        self.qStream=queue.Queue()

    def sensor_do(self, url, pf, buf):
        self.sensor.setopt(self.sensor.URL, url) 
        self.sensor.setopt(self.sensor.POSTFIELDS, pf) 
        self.sensor.setopt(self.sensor.WRITEDATA, buf) 
        retriesLeft = 3
        delayBetweenRetries = 5 # seconds
        while retriesLeft > 0:
            try:
                self.sensor.perform() 
                rcode = self.sensor.getinfo(self.sensor.RESPONSE_CODE) 
                success = rcode in range(200, 207) 
                logger.info(f"{url} {pf}: {rcode} ({'OK' if success else 'ERROR'})") 
                return success
            except Exception as e:
                logger.warning(e)
                logger.info(f"retrying after {delayBetweenRetries}s.")
                retriesLeft -= 1
                time.sleep(delayBetweenRetries)    
        return False
    
    def launch(self):
        print(f"Launch the device {self.model} at {self.lidarip}:")
        rc = self.sensor_do(self.Base_URL+'reset', urlencode({'data':'reset_system'}), self.buffer) 
        if rc: 
            time.sleep(5) 
            rc = self.sensor_do(self.Base_URL+'setting', urlencode({'returns':self.retrunMode}), self.buffer)
        if rc: 
            time.sleep(5) 
            rc = self.sensor_do(self.Base_URL+'setting', urlencode({'rpm':self.rpm}), self.buffer) 
        if rc: 
            time.sleep(10) 
            rc = self.sensor_do(self.Base_URL+'setting', urlencode({'laser':'on'}), self.buffer) 
            
        http = urllib3.PoolManager()
        response = http.request('GET',self.Base_URL+"status.json")
        if response: 
            status = json.loads(response.data) 
            print (f"Sensor laser is {status['laser']['state']}, motor rpm is {status['motor']['rpm']}")
    
    def stop(self):
        print(f"Stopping the devide {self.model} at {self.lidarip}:")
        rc = self.sensor_do(self.Base_URL+'setting', urlencode({'rpm':'0'}), self.buffer) 
        if rc: 
            time.sleep(2) 
            rc = self.sensor_do(self.Base_URL+'setting', urlencode({'laser':'off'}), self.buffer) 
        if rc: 
            time.sleep(10)
            http = urllib3.PoolManager()
            response = http.request('GET',self.Base_URL+"status.json")
            if response: 
                status = json.loads(response.data) 
                print (f"Sensor laser is {status['laser']['state']}, motor rpm is {status['motor']['rpm']}")
        self.sensor.close()

    def read_live_data(self):
        """
        https://github.com/valgur/velodyne_decoder/issues/4#issuecomment-1248660033
        """
        while True:
            data, address = self.socket.recvfrom(vd.PACKET_SIZE * 2)
            recv_stamp = time.time()
            yield self.decoder.decode(recv_stamp, data, self.as_pcl_structs)
            
    def stream2pcap(self, timestamp:float=None):
        data, address = self.socket.recvfrom(vd.PACKET_SIZE * 2)
        # print(f"recvfrom {address}")
        # print(f"data={data}")
        recv_stamp = time.time() if timestamp==None else timestamp
        decodedData=self.decoder.decode(recv_stamp, data, self.as_pcl_structs)
        if decodedData is not None:
            packet = (
                Ether(src='ff:ff:ff:ff:ff:ff',dst='ff:ff:ff:ff:ff:ff') # dst mac addr is broadcast with no doubt, but the src mac addr is also broadcast from the pcap file recorded by veloview. This would not affect the function of captured pcap file.
                / IP(src='192.168.0.200',dst='255.255.255.255') # src ip should be the ip of the lidar but is 192.168.0.200 in the pcap file recorded by veloview. This would not affect the function of captured pcap file.
                / UDP(sport=address[1],dport=address[1],chksum=0) # checksum is set to 0 (ignore) according to the pcap file recorded by veloview.
                / data # use operator / to append the recieved data at last
                )
            # print(f"[0]decoded data: {decodedData}")
            stamp, points = decodedData
            # print(f"Num points: {len(points)}")
            return packet
        else:
            # print(f"[1]decoded data: {decodedData}")
            return None
        
    def _recvfrom(self):
        while not exitFlag:
            self.qStream.put(item=(time.time(), *self.socket.recvfrom(vd.PACKET_SIZE * 2))) # push tuple (timeStamp, data, addr) to the queue
            
    def stream2pcap(self, baseThread:threading.Thread=None, filename:str=None, logger=None):
        assert baseThread is not None, f"Must specify baseThread (threading.Thread class) on which `q2pcap` is relied."
        assert filename is not None, f"Must specify filename."
        etherIPHead=(
                Ether(src='ff:ff:ff:ff:ff:ff',dst='ff:ff:ff:ff:ff:ff') # dst mac addr is broadcast with no doubt, but the src mac addr is also broadcast from the pcap file recorded by veloview. This would not affect the function of captured pcap file.
                / IP(src='192.168.0.200',dst='255.255.255.255') # src ip should be the ip of the lidar but is 192.168.0.200 in the pcap file recorded by veloview. This would not affect the function of captured pcap file.
                )
        pktWriter=PcapWriter(filename=filename,append=True)#,sync=True
        counter=0
        while True:
            if self.qStream.empty() and not baseThread.is_alive(): # exit thread
                break
            elif self.qStream.empty() and baseThread.is_alive(): # waiting for data
                # print(f"q is empty, continue.")
                continue
            else: # processing data
                oneTimestamp,oneData,oneAddress=self.qStream.get()
                if counter==0: # discard first (idx==0) sample due to its strange time difference to the 2nd sample
                    counter+=1
                    continue
                counter+=1
                packet = (
                    etherIPHead
                    / UDP(sport=oneAddress[1],dport=oneAddress[1],chksum=0) # checksum is set to 0 (ignore) according to the pcap file recorded by veloview.
                    / oneData # use operator / to append the recieved data at last
                    )
                packet.time=oneTimestamp
                logger.info(f"Write pcap of timestamp {oneTimestamp} to file, {self.qStream.qsize()} samples wait to be written.")
                pktWriter.write(packet)
        pktWriter.close()
                
        
def main(args):
    myld=ld(args.model,args.ip_lidar,args.dataport,args.rpm,args.returnmode)
    utcDate=datetime.now(timezone.utc).strftime('%Y%m%d')
    outdir=os.path.join(args.outdir,utcDate)
    if not os.path.exists(outdir): os.makedirs(outdir)
    logger.info(f"Outputs will be written to {outdir}.")
    myld.launch()
        
    if args.mode=='live':
        # live mode
        for Data in myld.read_live_data():
            if Data != None:
                utc_time=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%f')
                stamp, points = Data
                print(utc_time, points.shape)
                print(f"points:{points}")
                break
    elif args.mode=='pcap':
        # write mode
        utc_time=datetime.now(timezone.utc)
        filenamePrefix=args.model+'_'+str(args.rpm)+'rpm'+args.returnmode.capitalize()+'_'
    
        # log
        fileHandler = logging.FileHandler(os.path.join(outdir,filenamePrefix+utc_time.strftime('%Y%m%dT%H%M%S.%f')+'.log')) # 输出到文件的handler
        fileHandler.setFormatter(formatter)
        logger.addHandler(fileHandler)
    
        threadList=[]
        threadRecv=threading.Thread(target=myld._recvfrom,name='_recvfrom')
        threadList.append(threadRecv)
        pcapFilename=os.path.join(outdir,filenamePrefix+utc_time.strftime('%Y%m%dT%H%M%S.%f')+'.pcap')
        threadStream2pcap=threading.Thread(target=myld.stream2pcap,name='stream2pcap',args=(threadRecv,pcapFilename,logger))
        threadList.append(threadStream2pcap)
        for oneThread in threadList:
            logger.info(f"Starting thread:\t{oneThread.name}.")
            oneThread.start()
            
        try:
            time.sleep(60) # seconds
        except KeyboardInterrupt:
            logger.info("User interruption.")
        finally:
            global exitFlag
            exitFlag=True
            
        for oneThread in threadList:
            oneThread.join()
            logger.info(f"Thread {oneThread.name} stopped.")
    
    myld.stop()
                
        
if __name__=="__main__":
    parser=argparse.ArgumentParser(description="read data directly from velodyne lidar")
    parser.add_argument('--model', default='VLP-16', type=str,metavar="MODEL", help="Model of the velodyne lidar you use.")
    parser.add_argument('--ip-lidar', default='192.168.1.201', type=str,metavar="Lidar_IP", help="IP addr of velodyne lidar. Default:'192.168.1.201'.")
    parser.add_argument('--ip-local', default='', type=str,metavar="localhost", help="IP addr of localhost, not the velodyne lidar. Default:''(listen to all).")
    parser.add_argument('--dataport', default=2368, type=int,metavar="PORT", help="Data port to be listened for UDP packages. Default: 2368.")
    parser.add_argument('--rpm', default=1200, type=int,metavar="RPM", help="RPM of the velodyne lidar to be set.")
    parser.add_argument('--returnmode',default='dual',choices=['strongest','last','dual'],type=str,metavar="ReturnMode", help="pcap: read data and write to pcap file; live: read data in stream mode.")
    parser.add_argument('--mode',default='pcap',choices=['pcap','live'],type=str,metavar="MODE", help="pcap: read data and write to pcap file; live: read data in stream mode.")
    parser.add_argument('--outdir',default='out',type=str,metavar="DIR", help="output path.")
    args=parser.parse_args()
    main(args)