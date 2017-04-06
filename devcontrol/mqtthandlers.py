# mqtthandlers.py
# MQTT event processors
# 
# Part of AutoHome
#
# Copyright (c) 2017, Diego Guerrero
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The names of its contributors may not be used to endorse or promote products
#       derived from this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""MQTT event processors."""

import sys
import re
import shlex
import paho.mqtt.client as mqtt

import database
import device

_lobbyregex = re.compile(r"^([^/]+)/lobby$")
_adminregex = re.compile(r"^([^/]+)/admin$")

def onconnect(client, userdata, rc):
	"""MQTT connect callback."""
	
	if rc == 0:
		db     = userdata["database"]
		cursor = userdata["cursor"]
		
		client.subscribe("#", qos=0)
		users = database.userlist(cursor)
		
		for (username, _) in users:
			database.setconnected(cursor, username, False)
			client.publish(username + "/lobby", "ping")
		
		db.commit()
	else:
		userdata["done"] = True


def ondisconnect(client, userdata, rc):
	"""MQTT disconnect callback."""
	
	userdata["done"] = True

def onmessage(client, userdata, message):
	"""MQTT received message callback."""
	
	print(message.topic, "->", message.payload, file=sys.stderr)
	
	db        = userdata["database"]
	cursor    = userdata["cursor"]
	guestlist = userdata["guestlist"]
	match     = _lobbyregex.match(message.topic)
	
	try:
		data = message.payload.decode("utf8")
	except UnicodeDecodeError:
		print("error decoding payload as utf8", file=sys.stderr)
		print(file=sys.stderr)
		return
	
	if match:
		username = match.group(1)
		
		if database.exists_username(cursor, username):
			displayname = database.getdisplayname(cursor, username)
			
			if data == "hello" or data == "here":
				print("connected: " + shlex.quote(displayname))
				database.setconnected(cursor, username, True)
				
				if data == "hello":
					device.sync(userdata, displayname)
				
			elif data == "disconnected" or data == "abruptly disconnected":
				print("disconnected: " + shlex.quote(displayname))
				database.setconnected(cursor, username, False)
			
			db.commit()
		else:
			if data == "credentials please":
				guestlist.add(username)
			elif data == "disconnected" or data == "abruptly disconnected":
				guestlist.discard(username)
	
	match = _adminregex.match(message.topic)
	
	if match:
		username = match.group(1)
		
		if database.exists_username(cursor, username):
			if data.startswith("status"):
				database.setstatus(cursor, username, data[7:])  # strip "status "
				db.commit()
			elif data.startswith("schedule"):
				device.schedule_makeconsistent(userdata, username, data[9:])  # strip "schedule\n"
	
	print(file=sys.stderr)

def kickuser(configuration, username, password):
	"""Kick a user out of the MQTT network (must know user credentials)."""
	try:
		client = mqtt.Client()
		client.username_pw_set(username, password)
		client.tls_set(configuration["certificate"])
		client.connect(configuration["devhostname"], configuration["devmqttport"])
		
		client.publish(username + "/lobby", "disconnected", qos=0)
		# qos 0 because it will rarely fail (local client)
		# and if it fails it's no big deal, it will be replaced
		# the next time the device logs in
	finally:
		client.disconnect()
