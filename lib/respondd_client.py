#!/usr/bin/env python3

import socket
import select
import struct
import json
import zlib
import time

import lib.helper

from lib.ratelimit import rateLimit
from lib.nodeinfo import Nodeinfo
from lib.neighbours import Neighbours
from lib.statistics import Statistics

class ResponddClient:
  def __init__(self, config):
    self._config = config

    if 'rate_limit' in self._config:
      if 'rate_limit_burst' not in self._config:
        self._config['rate_limit_burst'] = 10
      self.__RateLimit = rateLimit(self._config['rate_limit'], self._config['rate_limit_burst'])
    else:
      self.__RateLimit = None

    self._nodeinfo = Nodeinfo(self._config)
    self._neighbours = Neighbours(self._config)
    self._statistics = Statistics(self._config)

  @staticmethod
  def joinMCAST(sock, addr, ifname):
    group = socket.inet_pton(socket.AF_INET6, addr)
    if_idx = socket.if_nametoindex(ifname)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, group + struct.pack('I', if_idx))

  def openSocket(self, interface, group):
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                    interface.encode() + bytes(1))
    sock.bind(('::', self._config['port']))
    self.joinMCAST(sock, group, interface)
    return sock

  def start(self):
    bridge = self._config['bridge']
    sockets = [self.openSocket(bridge, self._config['addr'])]

    lines = lib.helper.callBatctl([self._config['batman'], 'if'])
    for line in lines:
      interface, separator, _ = line.partition(':')
      if separator and interface.strip():
        sockets.append(self.openSocket(interface.strip(), 'ff02::2:1001'))

    while True:
      readable, _, _ = select.select(sockets, [], [])
      for sock in readable:
        msg, sourceAddress = sock.recvfrom(2048)
        requests = msg.decode('UTF-8').split(' ')

        if requests[0] == 'GET':
          response = {request: self.buildStruct(request)
                      for request in requests[1:]}
          self.sendStruct(sock, sourceAddress, response, True)
        else:
          response = self.buildStruct(requests[0])
          self.sendStruct(sock, sourceAddress, response, False)

  def buildStruct(self, responseType):
    if self.__RateLimit is not None and not self.__RateLimit.limit():
      print('rate limit reached!')
      return

    responseClass = None
    if responseType == 'statistics':
      responseClass = self._statistics
    elif responseType == 'nodeinfo':
      responseClass = self._nodeinfo
    elif responseType == 'neighbours':
      responseClass = self._neighbours
    else:
      print('unknown command: ' + responseType)
      return

    return responseClass.getStruct()

  def sendStruct(self, sock, destAddress, responseStruct, withCompression):
    if self._config['verbose'] or self._config['dry_run']:
      print('%14.3f %35s %5d: ' % (time.time(), destAddress[0], destAddress[1]), end='')
      print(json.dumps(responseStruct, sort_keys=True, indent=4))

    responseData = bytes(json.dumps(responseStruct, separators=(',', ':')), 'UTF-8')

    if withCompression:
      encoder = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15) # The data may be decompressed using zlib and many zlib bindings using -15 as the window size parameter.
      responseData = encoder.compress(responseData)
      responseData += encoder.flush()
      # return compress(str.encode(json.dumps(ret)))[2:-4] # bug? (mesh-announce strip here)

    if not self._config['dry_run']:
      sock.sendto(responseData, destAddress)
