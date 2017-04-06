# database.py
# Database interaction routines
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

"""Database interactino routines."""

import os, sys
import binascii
import hashlib
import shlex

from Event import Event

def _hashdigest(message, salt):
	""" Compute the hexadecimal digest of a message using the SHA256 algorithm."""
	
	processor = hashlib.sha256()
	
	processor.update(salt.encode("utf8"))
	processor.update(message.encode("utf8"))
	
	return processor.hexdigest()

def setupdb(cursor):
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

def setsuperuser(cursor, username, password):
	"""Add (or update) the super user credentials to the authorization database."""
	
	cursor.execute("insert or ignore into profile (username, displayname, type, connected, status) "
	               "values (?, ?, ?, ?, ?);", (username, "devmaster", "master", True, ""))
	cursor.execute("select hash, salt from auth where username = ?", (username,))
	stored = cursor.fetchone()
	
	# only update credentials if they were not found or the password is different
	if stored is None or stored[0] != _hashdigest(password, stored[1]):
		salt     = binascii.b2a_base64(os.urandom(32)).decode().strip("\n=")
		passhash = _hashdigest(password, salt)
		
		cursor.execute("insert or ignore into auth (username, hash, salt) "
		               "values (?, ?, ?);", (username, passhash, salt))
		cursor.execute("update auth set hash = ?, salt = ? where username = ?;",
		               (passhash, salt, username))

def getusername(cursor, displayname):
	"""Retrieve internal username from display name."""
	
	cursor.execute("select username from profile where displayname = ?;", (displayname,))
	
	username = cursor.fetchone()
	
	if username is None:
		return None
	
	return username[0]

def getdisplayname(cursor, username):
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

def rename(cursor, displayname, newdisplayname):
	"""Change the public display name of a device."""
	
	username = getusername(cursor, displayname)
	
	if username is None:
		return False
	
	cursor.execute("update or ignore profile set displayname = ? where displayname = ?;",
	               (newdisplayname, displayname))
	
	newname = getdisplayname(cursor, username)
	# to check the operation actually went through
	# otherwise the new named was already used
	
	return newname == newdisplayname

def addprofile(cursor, id, displayname=None, stype="device", connected=True, status=""):
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
	               "values (?, ?, ?, ?, ?);", (id, displayname, stype, connected, status))
	
	password = binascii.b2a_base64(os.urandom(32)).decode().strip('\n=')
	salt     = binascii.b2a_base64(os.urandom(32)).decode().strip('\n=')
	passhash = _hashdigest(password, salt)
	
	cursor.execute("insert or ignore into auth (username, hash, salt) "
	               "values (?, ?, ?);", (id, passhash, salt))
	
	return password

def delprofile(cursor, displayname):
	"""Remove the profile and credentials of a device from the database."""
	
	cursor.execute("delete from profile where displayname = ?;", (displayname,))

def setconnected(cursor, id, connected=True):
	"""Set the connected property in the device profile."""
	
	cursor.execute("update or ignore profile set connected = ? where username = ?;",
	               (int(bool(connected)), id))

def setstatus(cursor, id, status):
	"""Set the status of a device in its profile."""
	
	cursor.execute("update or ignore profile set status = ? where username = ?;", (status, id))

def addscheduled(cursor, username, event):
	"""Add an event to the schedule in the database."""
	
	fuzzy     = 1 if event.fuzzy     else 0
	recurrent = 1 if event.recurrent else 0
	
	cursor.execute("select count(*) from schedule where username = ? and command = ? and "
	               "fuzzy = ? and recurrent = ? and firedate = ? and weekday = ? and "
	               "hours = ? and minutes = ?;",
	               (username, event.command, fuzzy, recurrent, event.firedate,
	               event.weekday, event.hours, event.minutes))
	
	found = cursor.fetchone()
	
	if found[0] == 0:
		cursor.execute("insert or ignore into schedule values (NULL, ?, ?, ?, ?, ?, ?, ?, ?);",
		               (username, event.command, event.fuzzy, event.recurrent, event.firedate,
		               event.weekday, event.hours, event.minutes))
	else:
		print("this event is already in the schedule", file=sys.stderr)

def delscheduled(cursor, username, event):
	"""Remove an event from the schedule in the database."""
	
	fuzzy     = 1 if event.fuzzy     else 0
	recurrent = 1 if event.recurrent else 0
	
	cursor.execute("delete from schedule where username = ? and command = ? and fuzzy = ? and "
	               "recurrent = ? and firedate = ? and weekday = ? and hours = ? and minutes = ?;",
	               (username, event.command, fuzzy, recurrent, event.firedate,
	               event.weekday, event.hours, event.minutes))

def clearschedule(cursor, username):
	"""Clear any scheduled event for a particular username."""
	
	cursor.execute("delete from schedule where username = ?", (username,))

def devlist(cursor):
	"""Get a list of all known devices."""
	
	return [x for x in _devlist0(cursor)]

def _devlist0(cursor):
	"""Get a list of all known devices (iterator form)."""
	
	cursor.execute("select displayname, type, connected from profile;")
	
	info = cursor.fetchone()
	
	while info:
		if info[1] != "master":
			yield info[0], bool(int(info[2]))
		
		info = cursor.fetchone()

def userlist(cursor):
	"""Get a list of all users."""
	
	return [x for x in _userlist0(cursor)]

def _userlist0(cursor):
	"""Get a list of all known users (iterator form)."""
	
	cursor.execute("select username, type, connected from profile;")
	
	info = cursor.fetchone()
	
	while info:
		if info[1] != "master":
			yield info[0], bool(int(info[2]))
		
		info = cursor.fetchone()

def devinfo(cursor, displayname):
	"""Get profile information about a specific device."""
	
	cursor.execute("select type, connected, status from profile where displayname = ?;",
	               (displayname,))
	
	info = cursor.fetchone()
	
	if info is None:
		return None
	
	return info[0], bool(int(info[1])), info[2]

def devschedule(cursor, displayname):
	"""Get scheduled commands for a device."""
	
	return [x for x in _devschedule0(cursor, displayname)]

def _devschedule0(cursor, displayname):
	"""Get scheduled commands for a device (iterator form)."""
	
	username = getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	cursor.execute("select command, fuzzy, recurrent, firedate, weekday, "
	               "hours, minutes from schedule where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	while row:
		recurrent = (row[2] == 1)
		
		if recurrent:
			event = Event.create_recurrent(row[0], (row[1] == 1), row[4], row[5], row[6])
		else:
			event = Event.create_once(row[0], (row[1] == 1), row[3])
		
		yield event
		
		row = cursor.fetchone()
