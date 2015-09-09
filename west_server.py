#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import sys
import argparse
import select
from SocketServer import TCPServer
from websocket_server import WebsocketServer, WebSocketHandler
from http_proxy_client import http_proxy_http_client_thread
from west_parser import west_parser
from debug_tools import is_debug, debug_print

class ExtendedWebSocketHandler(WebSocketHandler):

    def handshake(self):
        '''
        override the original WebSocketHandler.handshake().
        to get the origin name from the WS request.
        to add some debug codes.
        '''
        try:
            message = self.request.recv(1024).decode().strip()
            if is_debug(2, self.server.west):
                print('DEBUG: ---BEGIN OF WEST MESSAGE---')
                print(message)
                print('DEBUG: ---END OF WEST MESSAGE---')
            headers = self.parse_request(message)
            upgrade = headers.get('upgrade')
            if upgrade != 'websocket':
                self.keep_alive = False
                return
            key = headers.get('sec-websocket-key')
            if not key:
                print("Client tried to connect but was missing a key")
                self.keep_alive = False
                return
            self.origin = headers.get('origin', '')
            if not self.origin:
                print('WARNING: Client does not send the origin.')
            #
            response = self.make_handshake_response(key)
            self.handshake_done = self.request.send(response.encode())
            self.valid_client = True
            self.server._new_client_(self)
        except Exception as e:
            print('ERROR: WebSocketHandler:', e)
            raise

    def parse_request(self, request):
        rs = request.split('\r\n')
        request = rs.pop(0)
        headers = {}
        for r in rs:
            k, v = r.split(':', 1)
            k = k.strip().lower()
            v = v.strip()
            headers.update({k : v})
        return headers

    def handle(self):
        try:
            while self.keep_alive:
                if not self.handshake_done:
                    self.handshake()
                elif self.valid_client:
                    self.read_next_message()
        except Exception as e:
            if e.message == 'need more than 0 values to unpack':
                pass
            else:
                print('ERROR:', e)

class west_server(WebsocketServer):

    '''
    extended to WebsocketServer() in websocket_server/websocket_server.py
    '''

    q_request = {}

    def __init__(self, west):
        self.west = west
        self.jc_mine = west.jc['wsts']
        TCPServer.__init__(self, (self.jc_mine['addr'], self.jc_mine['port']),
                           ExtendedWebSocketHandler)
        self.set_fn_new_client(self.new_client)
        self.set_fn_client_left(self.client_left)
        self.set_fn_message_received(self.message_received)

    def find_client_by_origin(self, origin):
        for i in self.clients:
            if i['origin'] == origin:
                return i
        return None

    # Called for every client connecting (before new_client())
    def _new_client_(self, handler):
        self.id_counter += 1
        client={
            'id'      : self.id_counter,
            'handler' : handler,
            'address' : handler.client_address,
            'origin'  : handler.origin
        }
        self.clients.append(client)
        self.new_client(client, self)

    # Called for every client connecting (after handshake)
    def new_client(self, client, server):
        #
        #if client['origin'] not in self.west.jc['ac']:
        #    print('ERROR: the WS client connection is not allowed. %s' %
        #          client['origin'])
        #    raise ValueError
        #
        print('INFO: WST client connected from %s origin=%s id=%d' %
              (repr(client['address']), client['origin'], client['id']))
        server.west.update_proxy_server_callback(self, client['origin'])

    # Called for every client disconnecting
    def client_left(self, client, server):
        print('INFO: WST client disconnected %s origin=%s id=%d' %
              (repr(client['address']), client['origin'], client['id']))
        server.west.remove_proxy_server_callback(self.west, client['origin'])

    # Called when a client sends a message
    def message_received(self, client, server, msg):
        if is_debug(1, self.west):
            print('DEBUG: received data len=%d from %s id=%d' % (
                    len(msg), repr(client), client['id']))
            if is_debug(2, self.west):
                print('DEBUG: ---BEGIN OF FORWARDED WSTC DATA---')
                print(msg)
                print('DEBUG: ---END OF FORWARDED WSTC DATA---')
        ret = west_parser(msg)
        if not ret:
            raise ValueError
        #
        t_origin = ret['wh'].get('TransactionOrigin')
        if not t_origin:
            print('ERROR: TransactionOrigin does not exist.')
            raise ValueError
        t_id = ret['wh'].get('TransactionID')
        if not t_id:
            print('ERROR: TransactionID does not exist.')
            return ValueError
        #
        proxy = self.q_request.get(t_id)
        if proxy:
            #
            # response from the server.
            #
            proxy.put_response(t_id, ret['hh'], ret['hc'])
            self.q_request.pop(t_id)
            return
        #
        # new request from the client.
        #
        if is_debug(1, self.west):
            print('DEBUG: t_origin, t_id = %s, %s' % (t_origin, t_id))
            if is_debug(2, self.west):
                print('DEBUG: ---BEGIN OF SENDING TO PROXY CLIENT---')
                print('DEBUG: wst headers=', ret['wh'])
                print('DEBUG: http request=', ret['hr'])
                print('DEBUG: http headers=', ret['hh'])
                print('DEBUG: http content=')
                print(ret['hc'])
                print('DEBUG: ---END OF SENDING TO PROXY CLIENT---')
        #
        http_proxy_http_client_thread(client, server, ret,
                                      debug_level=self.west.jc['debug_level'])

    def send(self, proxy, payload, session_id, proxy_protocol=''):
        client_origin = proxy.server.jc_mine['en']
        client = self.find_client_by_origin(client_origin)
        if not client:
            print('ERROR: no client for this proxy is associated. %s' %
                  client_origin)
            raise TypeError
        msg_list = []
        msg_list.append('TransactionOrigin: %s\r\n' %
              proxy.server.west.jc['wstc'][client_origin]['nm'])
        msg_list.append('TransactionID: %s\r\n' % session_id)
        if proxy_protocol:
            msg_list.append('X-Proxy-Protocol: %s\r\n' % proxy_protocol)
        msg_list.append('\r\n')
        msg_list.append(payload)
        msg = ''.join(msg_list)
        self.send_message(client, msg)
        if is_debug(1, self.west):
            print('DEBUG: sent wst message length=%d' % len(msg))
            if is_debug(2, self.west):
                print('DEBUG: ---BEGIN OF FORWARDING WST DATA---')
                print(msg)
                print('DEBUG: ---END OF FORWARDING WST DATA---')
        self.q_request[session_id] = proxy

    def reply(self, client, msg):
        self.send_message(client, msg)

'''
test code
'''
from west_config import west_config

class west_server_test():
    def __init__(self, config, debug_level=0):
        self.jc = west_config(sys.argv[1], debug_level=3)

    def go(self):
        west_server(self)
        self.jc.print_state()

if __name__ == '__main__' :
    if len(sys.argv) != 2:
        print('Usage: this (config)')
        print('    e.g. this config.json')
        exit(1)
    this = west_server_test(sys.argv[1])
    this.go()
