# autohome.py
# Home automation start script
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

import paho.mqtt.client as mqtt
import sqlite3
import os, sys, signal, subprocess
import json
import binascii
import hashlib
import re
import time
import select
import shlex
import logging, logging.handlers
import string
import traceback

# sensor definitions
# this defines the acceptable sensor types, their acceptable commands
# and the status each command leaves the sensor in;
#
# the command list or the status dict may be switch with lambda functions
# if more expressibility is required.
# In such case, the commands function commands(command) = True if command is valid else False
# should receive a command string and return a boolean indicating its validity;
# 
# status should be a function valid(status) = True if status is valid else False;
#
# transform should be a function transform(command, oldstatus) -> newstatus
# which receives a correct command and the previous status, and output a status type
# or None to signal no status change;
# note that status may also change from a message directly from the sensor
sensors = {
	"sonoff": {
		"commands": ["on", "off", "toggle"],
		"status": ["on", "off"],
		"transform": lambda cmd, status: cmd if (cmd == "on" or cmd == "off") else "on" if status == "off" else "off"
	}
}

# Exceptions
# ------------------------------------------------------------------------------

class FormatError(Exception):
	"""Formatting error. An input string couldn't be parsed as it did not conform to its schema."""
	
	def __init__(self, value):
		self.value = value
	
	def __str__(self):
		return repr(self.value)

# Signals
# ------------------------------------------------------------------------------

def setsignals():
	"""Set termination signals to gracefully close every resource."""
	signal.signal(signal.SIGINT,  gracefulexit)
	signal.signal(signal.SIGTERM, gracefulexit)
	signal.signal(signal.SIGHUP,  gracefulexit)

def gracefulexit(signal, frame):
	"""Close the database, broker and client, and exit the program."""
	
	sys.exit(0)  # resources will be closed on their corresponding finally blocks

# Database
# ------------------------------------------------------------------------------

def hash(message, salt):
	""" Compute the hexadecimal digest of a message using the SHA256 algorithm."""
	
	processor = hashlib.sha256()
	
	processor.update(salt.encode("utf8"))
	processor.update(message.encode("utf8"))
	
	return processor.hexdigest()

def setup_db(cursor):
	"""Set up the authorization database to conform to this service's schema.
	
	The schema consists of three tables: auth, profile and schedule. 'auth' maintains the
	MQTT credentials for every verified client. 'profile' maintains the identity
	details of every device including its type, visible name, MQTT username,
	connection status and sensor status. 'schedule' holds a list of scheduled events.
	"""
	
	cursor.execute("select count(*) from sqlite_master where type='table' and name='profile';")
	
	count = cursor.fetchone()
	
	if count[0] == 0:
		cursor.execute("create table if not exists profile ("
		               "  username text not null primary key,"
		               "  displayname text not null unique,"
		               "  type text not null,"
		               "  connected text not null,"
		               "  status text not null"
		               ");")
	
	cursor.execute("select count(*) from sqlite_master where type='table' and name='auth';")
	
	count = cursor.fetchone()
	
	if count[0] == 0:
		cursor.execute("create table if not exists auth ("
		               "  username text not null primary key references profile on delete cascade,"
		               "  hash text not null,"
		               "  salt text not null"
		               ");")
	
	
	cursor.execute("select count(*) from sqlite_master where type='table' and name='schedule';")
	
	count = cursor.fetchone()
	
	if count[0] == 0:
		cursor.execute("create table if not exists schedule ("
		               "  id integer not null primary key,"
		               "  username text not null references profile on delete cascade,"
		               "  command text not null,"
		               "  fuzzy int not null,"
		               "  recurrent int not null,"
		               "  firedate int not null,"
		               "  weekday int not null,"
		               "  hours int not null,"
		               "  minutes int not null"
		               ");")
	
	cursor.execute("pragma foreign_keys = on;")

def set_superuser(cursor, username, password):
	"""Add (or update) the super user credentials to the authorization database."""
	
	cursor.execute("insert or ignore into profile (username, displayname, type, connected, status) "
	               "values (?, ?, ?, ?, ?);", (username, "devmaster", "master", True, ""))
	cursor.execute("select hash, salt from auth where username = ?", (username,))
	stored = cursor.fetchone()
	
	# only update credentials if they were not found or the password is different
	if stored is None or stored[0] != hash(password, stored[1]):
		salt     = binascii.b2a_base64(os.urandom(32)).decode().strip("\n=")
		passhash = hash(password, salt)
		
		cursor.execute("insert or ignore into auth (username, hash, salt) "
		               "values (?, ?, ?);", (username, passhash, salt))
		cursor.execute("update auth set hash = ?, salt = ? where username = ?;", (passhash, salt, username))

def get_username(cursor, displayname):
	"""Retrieve internal username from display name."""
	
	cursor.execute("select username from profile where displayname = ?;", (displayname,))
	
	username = cursor.fetchone()
	
	if username is None:
		return None
	
	return username[0]

def get_displayname(cursor, username):
	"""Retrieve public display name from username."""
	
	cursor.execute("select displayname from profile where username = ?;", (username,))
	
	displayname = cursor.fetchone()
	
	if displayname is None:
		return None
	
	return displayname[0]

def exists_username(cursor, username):
	"""Check whether a username exists in the database."""
	
	cursor.execute("select username from profile where username = ?;", (username,))
	
	return cursor.fetchone() is not None

def exists_displayname(cursor, displayname):
	"""Check whether a display name exists in the database."""
	
	cursor.execute("select displayname from profile where displayname = ?;", (displayname,))
	
	return cursor.fetchone() is not None

def add_device_profile(cursor, id, displayname=None, type="device", connected=True, status=""):
	"""Add a device profile and authorization credentials to the database.
	
	Generate a profile and a password for the device in the database.
	The device should reconnect with its new credentials to start
	communicating relevant information.
	
	Returns:
		Assigned password for this device.
	"""
	
	if displayname is None:
		displayname = id
	
	if exists_username(cursor, id) or exists_displayname(cursor, displayname):
		return None
	
	connected = 1 if connected else 0
	
	cursor.execute("insert or ignore into profile (username, displayname, type, connected, status) "
	               "values (?, ?, ?, ?, ?);", (id, displayname, type, connected, status))
	
	password = binascii.b2a_base64(os.urandom(32)).decode().strip('\n=')
	salt     = binascii.b2a_base64(os.urandom(32)).decode().strip('\n=')
	passhash = hash(password, salt)
	
	cursor.execute("insert or ignore into auth (username, hash, salt) "
	               "values (?, ?, ?);", (id, passhash, salt))
	
	return password

def update_device_name(cursor, displayname, newdisplayname):
	"""Change the public display name of a device."""
	
	username = get_username(cursor, displayname)
	
	if username is None:
		return False
	
	cursor.execute("update or ignore profile set displayname = ? where displayname = ?;", (newdisplayname, displayname))
	
	newname = get_displayname(cursor, username)  # to check the operation actually went through
	                                             # otherwise the new named was already used
	
	return newname == newdisplayname

def del_device_profile(cursor, displayname):
	"""Remove the profile and credentials of a device from the database."""
	
	cursor.execute("delete from profile where displayname = ?;", (displayname,))

def set_connected(cursor, id, connected=True):
	"""Set the connected property in the device profile."""
	
	cursor.execute("update or ignore profile set connected = ? where username = ?;", (int(bool(connected)), id))

def set_status(cursor, id, status):
	"""Set the status of a device in its profile."""
	
	cursor.execute("update or ignore profile set status = ? where username = ?;", (status, id))

def add_scheduled(cursor, username, command, fuzzy=False, recurrent=False, firedate=None, weekday=None, hours=None, minutes=None):
	"""Add an event to the schedule in the database."""
	
	if recurrent:
		if weekday is None or hours is None or minutes is None:
			raise ValueError("Weekday, hours and minutes must be set in a recurrent event")
	else:
		if firedate is None:
			raise ValueError("Firedate must be set in a non-recurrent event")
	
	if firedate is not None and (weekday is not None or hours is not None or minutes is not None):
		raise ValueError("Can't give both a firedate and a recurrent specification")
	
	fuzzy     = 1 if fuzzy else 0
	recurrent = 1 if recurrent else 0
	firedate  = int(firedate) if firedate is not None else 0
	weekday   = int(weekday)  if weekday  is not None else 0
	hours     = int(hours)    if hours    is not None else 0
	minutes   = int(minutes)  if minutes  is not None else 0
	
	cursor.execute("select count(*) from schedule where username = ? and command = ? and fuzzy = ? and "
	               "  recurrent = ? and firedate = ? and weekday = ? and hours = ? and minutes = ?;",
	               (username, command, fuzzy, recurrent, firedate, weekday, hours, minutes))
	
	found = cursor.fetchone()
	
	if found[0] == 0:
		cursor.execute("insert or ignore into schedule values (NULL, ?, ?, ?, ?, ?, ?, ?, ?);",
		               (username, command, fuzzy, recurrent, firedate, weekday, hours, minutes))
	else:
		print("this event is already in the schedule", file=sys.stderr)

def del_scheduled(cursor, username, command, fuzzy=False, recurrent=False, firedate=None, weekday=None, hours=None, minutes=None):
	"""Remove an event from the schedule in the database."""
	
	if recurrent:
		if weekday is None or hours is None or minutes is None:
			raise ValueError("Weekday, hours and minutes must be set in a recurrent event")
	else:
		if firedate is None:
			raise ValueError("Firedate must be set in a non-recurrent event")
	
	if firedate is not None and (weekday is not None or hours is not None or minutes is not None):
		raise ValueError("Can't give both a firedate and a recurrent specification")
	
	fuzzy     = 1 if fuzzy else 0
	recurrent = 1 if recurrent else 0
	firedate  = int(firedate) if firedate is not None else 0
	weekday   = int(weekday)  if weekday  is not None else 0
	hours     = int(hours)    if hours    is not None else 0
	minutes   = int(minutes)  if minutes  is not None else 0
	
	cursor.execute("delete from schedule where username = ? and command = ? and fuzzy = ? and "
	               "recurrent = ? and firedate = ? and weekday = ? and hours = ? and minutes = ?;",
	               (username, command, fuzzy, recurrent, firedate, weekday, hours, minutes))

def clear_schedule(cursor, username):
	"""Clear any scheduled event for a particular username."""
	
	cursor.execute("delete from schedule where username = ?", (username,))

def devlist(cursor):
	"""Get a list of all known devices."""
	
	return [x for x in devlist0(cursor)]

def devlist0(cursor):
	"""Get a list of all known devices (iterator form)."""
	
	cursor.execute("select displayname, type, connected from profile;")
	
	devinfo = cursor.fetchone()
	
	while devinfo:
		if devinfo[1] != "master":
			yield devinfo[0], bool(int(devinfo[2]))
		
		devinfo = cursor.fetchone()

def userlist(cursor):
	"""Get a list of all users."""
	
	return [x for x in userlist0(cursor)]

def userlist0(cursor):
	"""Get a list of all known users (iterator form)."""
	
	cursor.execute("select username, type, connected from profile;")
	
	devinfo = cursor.fetchone()
	
	while devinfo:
		if devinfo[1] != "master":
			yield devinfo[0], bool(int(devinfo[2]))
		
		devinfo = cursor.fetchone()

def devinfo(cursor, displayname):
	"""Get profile information about a specific device."""
	
	cursor.execute("select type, connected, status from profile where displayname = ?;", (displayname,))
	
	info = cursor.fetchone()
	
	if info is None:
		return None
	
	return info[0], bool(int(info[1])), info[2]

def devschedule(cursor, displayname):
	"""Get scheduled commands for a device."""
	
	return [x for x in devschedule0(cursor, displayname)]

def devschedule0(cursor, displayname):
	"""Get scheduled commands for a device (iterator form)."""
	
	username = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	cursor.execute("select command, fuzzy, recurrent, firedate, weekday, hours, minutes from schedule where username = ?;", (username,))
	
	event = cursor.fetchone()
	
	while event:
		data = dict()
		data["command"]   = event[0]
		data["fuzzy"]     = event[1]
		data["recurrent"] = event[2]
		
		if data["recurrent"]:
			data["weekday"] = event[4]
			data["hours"]   = event[5]
			data["minutes"] = event[6]
		else:
			data["firedate"] = event[3]
		
		yield data
		
		event = cursor.fetchone()

# Device interaction
# ------------------------------------------------------------------------------

def valid_command(type, command):
	"""Check whether it is acceptable to send a given command to a device of the given type."""
	
	if type in sensors:
		if callable(sensors[type]["commands"]):
			return sensors[type]["commands"](command)
		else:
			return command in sensors[type]["commands"]
	
	return False

def valid_status(type, status):
	"""Check whether a status is acceptable for a device of a given type."""
	
	if type in sensors:
		if callable(sensors[type]["status"]):
			return sensors[type]["status"](status)
		else:
			return status in sensors[type]["status"]
	
	return False

def status_transform(type, command, status):
	"""Deduce the status that a command will leave a device in.
	
	The type and command must be correct, otherwise the behaviour is undefined.
	"""
	
	if type in sensors:
		if callable(sensors[type]["transform"]):
			return sensors[type]["transform"](command, status)
		else:
			return sensors[type]["transform"].get(command, None)
	
	return None

def add_device(userdata, id, displayname=None, type="device", status=""):
	"""Add a device profile to the database and notify the device of its new credentials."""
	
	database    = userdata["database"]
	cursor      = userdata["cursor"]
	client      = userdata["client"]
	credentials = userdata["credentials"]
	guestlist   = userdata["guestlist"]
	
	if id not in guestlist:
		print("there's no " + id + " in the lobby", file=sys.stderr)
		return
	
	if type not in sensors:
		print("not a valid device type", file=sys.stderr)
		return
	
	if not valid_status(type, status):
		print("not a valid status for the given device type", file=sys.stderr)
		return
	
	password = add_device_profile(cursor, id, displayname, type, True, status)
	
	database.commit()
	
	if password is None:
		print("username already taken", file=sys.stderr)
		return
	
	guestlist.discard(id)
	
	client.publish(id + "/lobby", "auth\n" + id + "\n" + password, qos=1)
	kick_user(credentials, id, password)

def rename_device(userdata, displayname, newdisplayname):
	"""Change the public display name of a device."""
	
	cursor      = userdata["cursor"]
	credentials = userdata["credentials"]
	username    = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	if username == credentials["username"]:
		print("can't rename super user", file=sys.stderr)
		return
	
	if not update_device_name(cursor, displayname, newdisplayname):
		print("can't rename the device, the new name may already be in use", file=sys.stderr)
		return

def del_device(userdata, displayname):
	"""Remove the profile and credentials of a device from the database."""
	
	cursor      = userdata["cursor"]
	database    = userdata["database"]
	credentials = userdata["credentials"]
	username    = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	if username == credentials["username"]:
		print("can't delete super user", file=sys.stderr)
		return
	
	del_device_profile(cursor, displayname)
	
	database.commit()
	
	kick_user(credentials, username, credentials["psk"])

def sync_device(userdata, displayname):
	"""Send a synchronization signal to a device to correct time drift."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	t = int(time.time())
	t = t - time.timezone if not time.daylight else t - time.altzone
	
	client.publish(username + "/admin", "time " + str(t), qos=1)

def ping_device(userdata, displayname):
	"""Check that a device is responsive by sending a ping request."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	client.publish(username + "/lobby", "ping", qos=0)

def askstatus_device(userdata, displayname):
	"""Ask the device for its current status."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	client.publish(username + "/admin", "askstatus", qos=0)

def exec_command(userdata, displayname, command):
	"""Send a signal to a device to execute a command.
	
	The command is first validated to check that the device can act on it, given its type.
	"""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
		
	cursor.execute("select type, status from profile where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	if row is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	type   = row[0]
	status = row[1]
	
	if not valid_command(type, command):
		print("invalid command " + str(command) + " for type " + str(type), file=sys.stderr)
		return
	
	newstatus = status_transform(type, command, status)
	
	if newstatus is not None:
		set_status(cursor, username, newstatus)
	
	client.publish(username + "/control", command, qos=1)  # should be qos 2 because it is the only operation
	                                                       # that may not be idempotent, but PubSubClient doesn't
	                                                       # support it (and it's too heavyweight anyway)

def schedule_once(userdata, displayname, firedate, fuzzy, command):
	"""Add an operation to be performed once at a particular time to a device's schedule."""
	
	cursor      = userdata["cursor"]
	client      = userdata["client"]
	credentials = userdata["credentials"]
	username    = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	if username == credentials["username"]:
		print("the schedule is for devices", file=sys.stderr)
		return
		
	cursor.execute("select type, status from profile where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	if row is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	type   = row[0]
	status = row[1]
	
	if not valid_command(type, command):
		print("invalid command " + str(command) + " for type " + str(type), file=sys.stderr)
		return
	
	firedate = int(firedate)
	
	client.publish(username + "/control", "timed +" + ('z' if fuzzy else 'x') + " " + str(int(firedate)) + " " + command, qos=1)
	
	add_scheduled(cursor, username, command, fuzzy, recurrent=False, firedate=firedate)

def schedule_recurrent(userdata, displayname, weekday, hours, minutes, fuzzy, command):
	"""Add an operation to be performed recurrently to a device's schedule."""
	
	cursor      = userdata["cursor"]
	client      = userdata["client"]
	credentials = userdata["credentials"]
	username    = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	if username == credentials["username"]:
		print("the schedule is for devices", file=sys.stderr)
		return
		
	cursor.execute("select type, status from profile where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	if row is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	type   = row[0]
	status = row[1]
	
	if not valid_command(type, command):
		print("invalid command " + str(command) + " for type " + str(type), file=sys.stderr)
		return
	
	weekday = int(weekday)
	hours   = int(hours)
	minutes = int(minutes)
	
	if weekday < 0 or weekday > 9 or hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
		print("incorrect recurrent schedule specification", file=sys.stderr)
		return
	
	client.publish(username + "/control", "recurrent +" + ('z' if fuzzy else 'x') + str(weekday) + " "
	               + "{0:02d}.{1:02d}".format(hours, minutes) + " " + str(command), qos=1)
	
	add_scheduled(cursor, username, command, fuzzy, recurrent=True, weekday=weekday, hours=hours, minutes=minutes)

def unschedule_once(userdata, displayname, firedate, fuzzy, command):
	"""Remove an operation to be performed once at a particular time from a device's schedule."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
		
	cursor.execute("select type, status from profile where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	if row is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	type   = row[0]
	status = row[1]
	
	if not valid_command(type, command):
		print("invalid command " + str(command) + " for type " + str(type), file=sys.stderr)
		return
	
	firedate = int(firedate)
	
	client.publish(username + "/control", "timed -" + ('z' if fuzzy else 'x') + " " + str(int(firedate)) + " " + command, qos=1)
	
	del_scheduled(cursor, username, command, fuzzy, recurrent=False, firedate=firedate)

def unschedule_recurrent(userdata, displayname, weekday, hours, minutes, fuzzy, command):
	"""Remove an operation to be performed recurrently from a device's schedule."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
		
	cursor.execute("select type, status from profile where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	if row is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	type   = row[0]
	status = row[1]
	
	if not valid_command(type, command):
		print("invalid command " + str(command) + " for type " + str(type), file=sys.stderr)
		return
	
	weekday = int(weekday)
	hours   = int(hours)
	minutes = int(minutes)
	
	if weekday < 0 or weekday > 9 or hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
		print("incorrect recurrent schedule specification", file=sys.stderr)
		return
	
	client.publish(username + "/control", "recurrent -" + ('z' if fuzzy else 'x') + str(weekday) + " "
	               + "{0:02d}.{1:02d}".format(hours, minutes) + " " + str(command), qos=1)
	
	del_scheduled(cursor, username, command, fuzzy, recurrent=True, weekday=weekday, hours=hours, minutes=minutes)

def clear_device_schedule(userdata, displayname):
	"""Remove all events for a device from the local database and the device's database."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = get_username(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	client.publish(username + "/control", "clear", qos=1)
	clear_schedule(cursor, username)

# MQTT
# ------------------------------------------------------------------------------

lobbyregex = re.compile(r"^([^/]+)/lobby$")
adminregex = re.compile(r"^([^/]+)/admin$")

def onconnect(client, userdata, rc):
	"""MQTT connect callback."""
	
	if rc == 0:
		database = userdata["database"]
		cursor   = userdata["cursor"]
		
		client.subscribe("#", qos=0)
		users = userlist(cursor)
		
		for (username, connected) in users:
			set_connected(cursor, username, False)
			client.publish(username + "/lobby", "ping")
		
		database.commit()
	else:
		userdata["done"] = True


def ondisconnect(client, userdata, rc):
	"""MQTT disconnect callback."""
	
	userdata["done"] = True

def onmessage(client, userdata, message):
	"""MQTT received message callback."""
	
	print(message.topic, "->", message.payload, file=sys.stderr)
	
	database  = userdata["database"]
	cursor    = userdata["cursor"]
	guestlist = userdata["guestlist"]
	match     = lobbyregex.match(message.topic)
	
	try:
		data = message.payload.decode("utf8")
	except UnicodeDecodeError:
		print("error decoding payload as utf8", file=sys.stderr)
		print(file=sys.stderr)
		return
	
	if match:
		username = match.group(1)
		
		if exists_username(cursor, username):
			displayname = get_displayname(cursor, username)
			
			if data == "hello" or data == "here":
				print("connected: " + shlex.quote(displayname))
				set_connected(cursor, username, True)
				
				if data == "hello":
					sync_device(userdata, displayname)
				
			elif data == "disconnected" or data == "abruptly disconnected":
				print("disconnected: " + shlex.quote(displayname))
				set_connected(cursor, username, False)
			
			database.commit()
		else:
			if data == "credentials please":
				guestlist.add(username)
			elif data == "disconnected" or data == "abruptly disconnected":
				guestlist.discard(username)
	
	match = adminregex.match(message.topic)
	
	if match:
		username = match.group(1)
		
		if exists_username(cursor, username):
			if data.startswith("status"):
				set_status(cursor, username, data[7:])  # strip "status "
				database.commit()
	
	print(file=sys.stderr)

def kick_user(credentials, username, password):
	"""Kick a user out of the MQTT network (must know user credentials)."""
	try:
		client = mqtt.Client()
		client.username_pw_set(username, password)
		client.tls_set(credentials["certificate"])
		client.connect(credentials["hostname"], credentials["port"])
		
		client.publish(username + "/lobby", "disconnected", qos=0)  # qos 0 because it will rarely fail (local client)
		                                                            # and if it fails it's no big deal, it will be replaced
		                                                            # the next time the device logs in
	finally:
		client.disconnect()

# Main routines
# ------------------------------------------------------------------------------

def processline(userdata, line):
	"""Read a line from stdin and execute the corresponding command."""
	
	# commands should use double quotes if an argument has spaces in it and escape internal double quotes as necessary
	tokens = shlex.split(line)
	if len(tokens) < 1:
		return
	
	command = tokens[0]
	args    = tokens[1:]
	
	
	if command == "add":
		# add <guestname> <displayname> <type> <status>
		# add a device to the network
		
		if len(args) != 4:
			raise FormatError("Wrong number of arguments for 'add', expected 4 but got " + str(len(args)))
		
		add_device(userdata, args[0], args[1], args[2], args[3])
		
	elif command == "rename":
		# rename <displayname> <newdisplayname>
		# change the public display name of a device in the network
		
		if len(args) != 2:
			raise FormatError("Wrong number of arguments for 'del', expected 1 but got " + str(len(args)))
		
		rename_device(userdata, args[0], args[1])
		
	elif command == "del":
		# del <displayname>
		# delete a device from the network
		
		if len(args) != 1:
			raise FormatError("Wrong number of arguments for 'del', expected 1 but got " + str(len(args)))
		
		del_device(userdata, args[0])
		
	elif command == "sync":
		# sync <displayname>
		# send a time synchronization message to the specified device
		
		if len(args) != 1:
			raise FormatError("Wrong number of arguments for 'sync', expected 1 but got " + str(len(args)))
		
		sync_device(userdata, args[0])
		
	elif command == "ping":
		# ping <displayname>
		# check a device is still responsive by sending a ping message
		
		if len(args) != 1:
			raise FormatError("Wrong number of arguments for 'ping', expected 1 but got " + str(len(args)))
		
		ping_device(userdata, args[0])
		
	elif command == "askstatus":
		# askstatus <displayname>
		# ask the device for its current status
		
		if len(args) != 1:
			raise FormatError("Wrong number of arguments for 'status', expected 1 but got " + str(len(args)))
		
		askstatus_device(userdata, args[0])
		
	elif command == "cmd":
		# cmd <displayname> <operation>
		# send immediate command to device;
		# arguments to the operation should be within the operation argument, e.g. 'dimmer 126'
		# the operation must be valid (and have valid arguments) for the device type;
		# otherwise, the command will fail silently
		
		if len(args) != 2:
			raise FormatError("Wrong number of arguments for 'cmd', expected 2 but got " + str(len(args)))
		
		exec_command(userdata, args[0], args[1])
		
	elif command == "timed":
		# timed (add|del) <displayname> <date> (exact|fuzzy) <operation>
		# schedule a command for the future;
		# 'add' indicates to add the operation to the schedule, 'del' indicates to remove it from it;
		# <date> must be formatted as a unix integer timestamp in the future,
		# otherwise the command is a no-op;
		# 'exact' sets the timer for the specific timestamp; 'fuzzy' adds a small amount of time noise;
		# <operation> and <args> follow the same rules as the 'cmd' message;
		
		if len(args) != 5:
			raise FormatError("Wrong number of arguments for 'timed', expected 5 but got " + str(len(args)))
		
		fuzzy = False
		
		if args[3] == "exact":
			fuzzy = False
		elif args[3] == "fuzzy":
			fuzzy = True
		else:
			raise FormatError("Expected 'fuzzy' or 'exact' but found '" + args[3] + "' instead.")
		
		if args[0] == "add":
			schedule_once(userdata, args[1], int(args[2]), fuzzy, args[4])
		elif args[0] == "del":
			unschedule_once(userdata, args[1], int(args[2]), fuzzy, args[4])
		else:
			raise FormatError("Expected 'add' or 'del' but found '" + args[0] + "' instead.")
		
	elif command == "recurrent":
		# recurrent (add|del) <displayname> <weekday> <hours> <minutes> (exact|fuzzy) <operation>
		# schedule a recurrent command for the future;
		# 'add' indicates to add the operation to the schedule, 'del' indicates to remove it from it;
		# <weekday> must be a number between 0 and 9. Passing 0 signals the operation should execute
		# every day; 1-7 signal it should be executed on Mon-Sun respectively; 8 signals Mon-Fri; and
		# 9 signals Sat-Sun;
		# 'exact' sets the timer for the specific timestamp; 'fuzzy' adds a small amount of time noise;
		# <operation> and <args> follow the same rules as the 'cmd' message
		
		if len(args) != 7:
			raise FormatError("Wrong number of arguments for 'recurrent', expected 7 but got " + str(len(args)))
		
		fuzzy = False
		
		if args[5] == "exact":
			fuzzy = False
		elif args[5] == "fuzzy":
			fuzzy = True
		else:
			raise FormatError("Expected 'fuzzy' or 'exact' but found '" + args[5] + "' instead.")
		
		if args[0] == "add":
			schedule_recurrent(userdata, args[1], int(args[2]), int(args[3]), int(args[4]), fuzzy, args[6])
		elif args[0] == "del":
			unschedule_recurrent(userdata, args[1], int(args[2]), int(args[3]), int(args[4]), fuzzy, args[6])
		else:
			raise FormatError("Expected 'add' or 'del' but found '" + args[0] + "' instead.")
		
	elif command == "clear":
		# clear <displayname>
		# clear the schedule for a given device
		
		if len(args) != 1:
			raise FormatError("Wrong number of arguments for 'clear', expected 1 but got " + str(len(args)))
		
		clear_device_schedule(userdata, args[0])
		
	elif command == "devlist":
		# devlist
		# retrieve verified device list
		# respond with a list of devices, using the format: ('(-|+)<displayname>' )*
		# where every name is prepended with a positive sign '+' if the device is connected
		# and a negative sign '-' if it is not
		
		if len(args) != 0:
			raise FormatError("Wrong number of arguments for 'devlist', expected none but got " + str(len(args)))
		
		for (device, connected) in devlist(userdata["cursor"]):
			if device == "devmaster":
				continue
			
			if connected:
				print(shlex.quote("+" + device), end=" ")
			else:
				print(shlex.quote("-" + device), end=" ")
		
		print()
		
	elif command == "guestlist":
		# guestlist
		# retrieve the guestlist
		# respond with a list of unverified devices, using the format ('<displayname>' )*
		# note that every guest is connected (otherwise it would just be removed from the list)
		
		if len(args) != 0:
			raise FormatError("Wrong number of arguments for 'guestlist', expected none but got " + str(len(args)))
		
		for guest in userdata["guestlist"]:
			print(shlex.quote(guest), end=" ")
		
		print()
		
	elif command == "info":
		# info <displayname>
		# retrieve device profile
		# respond with the list of device properties in the profile using the format
		# <connected><type> <status>
		# where connected is formatted as + if the device is connected as - if it is not, e.g.
		# '-sonoff on' indicates a disconnected sonoff device with a status of 'on'
		
		if len(args) != 1:
			raise FormatError("Wrong number of arguments for 'info', expected 1 but got " + str(len(args)))
		
		type, connected, status = devinfo(userdata["cursor"], args[0])
		
		print(shlex.quote(("+" if connected else "-") + type), end=" ")
		print(shlex.quote(status))
		
	elif command == "schedule":
		# schedule <displayname>
		# retrieve device schedule
		# respond with a list of scheduled commands for the given device using the formats
		# timed <date> (exact|fuzzy) <operation> <args>
		# recurrent <weekday> <hours> <minutes> (exact|fuzzy) <operation> [<args>]
		# for timed and recurrent operations respectively;
		# the specifics of each formats are the same as for their schedule counterparts
		
		if len(args) != 1:
			raise FormatError("Wrong number of arguments for 'schedule', expected 1 but got " + str(len(args)))
		
		for event in devschedule(userdata["cursor"], args[0]):
			fztext = "fuzzy" if event["fuzzy"] else "exact"
			
			if event["recurrent"]:
				print("recurrent", shlex.quote(str(event["weekday"])), shlex.quote(str(event["hours"])),
				      shlex.quote(str(event["minutes"])), shlex.quote(fztext), shlex.quote(event["command"]))
			else:
				print("timed", shlex.quote(str(event["firedate"])),
				      shlex.quote(fztext), shlex.quote(event["command"]))
		
		print("")
	else:
		print("Unrecognized command, skipping", file=sys.stderr)

def genconfig(infilename, definitions, outfilename):
	with open(infilename, "r") as infile:
		text = infile.read()
	
	template = string.Template(text)
	text     = template.safe_substitute(definitions)
	
	with open(outfilename, "w") as outfile:
		outfile.write(text)

def main():
	"""Program entry point."""
	#global database, mosquitto, client
	
	setsignals()
	
	credentials = None
	guestlist   = set()
	
	try:
		with open("credentials.json") as credfile:
			credentials = json.loads(credfile.read())
	except IOError:
		print("Can't open credentials.json", file=sys.stderr)
		return
	
	with sqlite3.connect(credentials["dbfile"]) as database:
		cursor = database.cursor()
		
		try:
			setup_db(cursor)
			set_superuser(cursor, credentials["username"], credentials["password"])
		except:
			print("Can't set up the database and super user", file=sys.stderr)
			return
		
		database.commit()
		
		genconfig("mosquitto.conf.in", credentials, "mosquitto.conf")
		
		with subprocess.Popen(["mosquitto", "-c", "mosquitto.conf"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as mosquitto:
			time.sleep(0.5)  # let mosquitto broker start up
			
			handler = None
			
			try:
				logger  = logging.getLogger(__name__)
				handler = logging.handlers.RotatingFileHandler("mosquitto.log", "a", 1024 * 1024, 10)
				handler.setFormatter(logging.Formatter())
				logger.setLevel(logging.DEBUG)
				logger.addHandler(handler)
				
				print("MQTT broker started", file=sys.stderr)
				
				userdata = {"done": False, "database": database, "cursor": cursor, "credentials": credentials, "client": None, "guestlist": guestlist}
				client   = None
				
				try:
					client = mqtt.Client(userdata=userdata)
					client.username_pw_set(credentials["username"], credentials["password"])
					client.on_connect    = onconnect
					client.on_disconnect = ondisconnect
					client.on_message    = onmessage
					userdata["client"]   = client
					
					client.tls_set(credentials["certificate"])
					client.connect(credentials["hostname"], credentials["port"], keepalive=300)
					mqttsocket = client.socket()
					
					print("AutoHome public interface started", file=sys.stderr)
					
					while not userdata["done"]:
						try:
							# timeout is set to a value lesser than the keepalive period, otherwise
							# loop_misc() may not be called on time and the server can close the connection
							available = select.select([sys.stdin, mqttsocket, mosquitto.stderr], [], [], 30)
							
							if mqttsocket in available[0]:
								client.loop_read()
							
							if sys.stdin in available[0]:
								try:
									processline(userdata, input())
									database.commit()
								except FormatError as e:
									print("Error processing line: " + str(e), file=sys.stderr)
								except EOFError as e:
									userdata["done"] = True
							
							if client.want_write():
								client.loop_write()
							
							client.loop_misc()
							
							if mosquitto.stderr in available[0]:
								line = mosquitto.stderr.readline()
								logger.debug(line.strip().decode("utf8"))
							
						except (KeyboardInterrupt, SystemExit):
							raise
						except Exception as e:
							print("Unexpected exception", file=sys.stderr)
							print(e, file=sys.stderr)
							traceback.print_tb(e.__traceback__, file=sys.stderr)
							time.sleep(10)  # instead of crashing and losing everything, try to continue;
							                # if the problem was transient, the error is logged and the 
							                # system is still available; if the problem is fatal,
							                # wait so as to not generate infinite logfiles with
							                # succesive exceptions, e.g. because the sql database has
							                # been corrupted and every attempt to access it fails
				finally:
					client.disconnect()
			finally:
				mosquitto.terminate()
				
				if handler is not None:
					remaining = mosquitto.stderr.read()
					logger.debug(remaining.strip().decode("utf8"))
					handler.close()

if __name__ == "__main__":
	main()