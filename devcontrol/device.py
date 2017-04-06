# device.py
# Device interaction and database synchronization
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

"""Device interaction and database synchronization."""

import sys
import time
import shlex

from sensors import sensors
from Event import Event
import database
import mqtthandlers

def _validcommand(stype, command, status):
	"""Check whether it is acceptable to send a given command
	to a device of the given type and status.
	"""
	
	if stype in sensors:
		if callable(sensors[stype]["commands"]):
			return sensors[stype]["commands"](command, status)
		else:
			return command in sensors[stype]["commands"]
	
	return False

def _validstatus(stype, status):
	"""Check whether a status is acceptable for a device of a given type."""
	
	if stype in sensors:
		if callable(sensors[stype]["status"]):
			return sensors[stype]["status"](status)
		else:
			return status in sensors[stype]["status"]
	
	return False

def _statustransform(stype, command, status):
	"""Deduce the status that a command will leave a device in.
	
	The type and command must be correct, otherwise the behaviour is undefined.
	"""
	
	if stype in sensors:
		if callable(sensors[stype]["transform"]):
			return sensors[stype]["transform"](command, status)
		else:
			return sensors[stype]["transform"].get(command, None)
	
	return None

def add(userdata, id, displayname=None, stype="device", status=""):
	"""Add a device profile to the database and notify the device of its new credentials."""
	
	db            = userdata["database"]
	cursor        = userdata["cursor"]
	client        = userdata["client"]
	configuration = userdata["configuration"]
	guestlist     = userdata["guestlist"]
	
	if id not in guestlist:
		print("there's no " + id + " in the lobby", file=sys.stderr)
		return
	
	if stype not in sensors:
		print("not a valid device type", file=sys.stderr)
		return
	
	if not _validstatus(stype, status):
		print("not a valid status for the given device type", file=sys.stderr)
		return
	
	password = database.addprofile(cursor, id, displayname, stype, True, status)
	
	db.commit()
	
	if password is None:
		print("username already taken", file=sys.stderr)
		return
	
	guestlist.discard(id)
	
	client.publish(id + "/lobby", "auth\n" + id + "\n" + password, qos=1)
	mqtthandlers.kickuser(configuration, id, password)

def rename(userdata, displayname, newdisplayname):
	"""Change the public display name of a device."""
	
	cursor        = userdata["cursor"]
	configuration = userdata["configuration"]
	username      = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	if username == configuration["superuser"]:
		print("can't rename super user", file=sys.stderr)
		return
	
	if not database.rename(cursor, displayname, newdisplayname):
		print("can't rename the device, the new name may already be in use", file=sys.stderr)
		return

def delete(userdata, displayname):
	"""Remove the profile and credentials of a device from the database."""
	
	cursor        = userdata["cursor"]
	db            = userdata["database"]
	configuration = userdata["configuration"]
	username      = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	if username == configuration["superuser"]:
		print("can't delete super user", file=sys.stderr)
		return
	
	database.delprofile(cursor, displayname)
	
	db.commit()
	
	mqtthandlers.kickuser(configuration, username, configuration["devmqttpsk"])

def sync(userdata, displayname):
	"""Send a synchronization signal to a device to correct time drift."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	t = int(time.time())
	t = t - time.timezone if not time.daylight else t - time.altzone
	
	client.publish(username + "/admin", "time " + str(t), qos=1)

def ping(userdata, displayname):
	"""Check that a device is responsive by sending a ping request."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	client.publish(username + "/lobby", "ping", qos=0)

def askstatus(userdata, displayname):
	"""Ask the device for its current status."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	client.publish(username + "/admin", "askstatus", qos=0)

def execute(userdata, displayname, command):
	"""Send a signal to a device to execute a command.
	
	The command is first validated to check that the device can act on it, given its type.
	"""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
		
	cursor.execute("select type, status from profile where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	if row is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	stype  = row[0]
	status = row[1]
	
	if not _validcommand(stype, command, status):
		print("invalid command " + str(command) + " for type " + str(stype) +
		      " and status " + str(status), file=sys.stderr)
		return
	
	newstatus = _statustransform(stype, command, status)
	
	if newstatus is not None:
		database.setstatus(cursor, username, newstatus)
	
	# the next call should be qos 2 because it is the only operation
	# that may not be idempotent, but PubSubClient doesn't support it
	# (and it's too heavyweight anyway)
	client.publish(username + "/control", command, qos=1)

def schedule(userdata, displayname, event):
	"""Add an operation to be performed represented in an event dictionary to a device's schedule."""
	
	cursor        = userdata["cursor"]
	client        = userdata["client"]
	configuration = userdata["configuration"]
	username      = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	if username == configuration["superuser"]:
		print("the schedule is for devices", file=sys.stderr)
		return
		
	cursor.execute("select type, status from profile where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	if row is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	stype  = row[0]
	status = row[1]
	
	if not _validcommand(stype, event.command, status):
		print("invalid command " + str(event.command) + " for type " + str(stype) +
		      " and status " + str(status), file=sys.stderr)
		return
		
	eventstr = str(event)
	
	if event.recurrent:
		eventstr = eventstr[:10] + "+" + eventstr[10:]
	else:
		eventstr = eventstr[:6] + "+" + eventstr[6:]
	
	client.publish(username + "/control", eventstr, qos=1)
	database.addscheduled(cursor, username, event)

def unschedule(userdata, displayname, event):
	"""Add an operation to be performed represented in an event dictionary to a device's schedule."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
		
	cursor.execute("select type, status from profile where username = ?;", (username,))
	
	row = cursor.fetchone()
	
	if row is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	stype  = row[0]
	status = row[1]
	
	if not _validcommand(stype, event.command, status):
		print("invalid command " + str(event.command) + " for type " + str(stype) +
		      " and status " + str(status), file=sys.stderr)
		return
		
	eventstr = str(event)
	
	if event.recurrent:
		eventstr = eventstr[:10] + "-" + eventstr[10:]
	else:
		eventstr = eventstr[:6] + "-" + eventstr[6:]
	
	client.publish(username + "/control", eventstr, qos=1)
	database.delscheduled(cursor, username, event)

def clearschedule(userdata, displayname):
	"""Remove all events for a device from the local database and the device's database."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	client.publish(username + "/control", "clear", qos=1)
	database.clearschedule(cursor, username)

def askschedule(userdata, displayname):
	"""Ask the device for its current schedule."""
	
	cursor   = userdata["cursor"]
	client   = userdata["client"]
	username = database.getusername(cursor, displayname)
	
	if username is None:
		print("can't find user " + shlex.quote(str(displayname)), file=sys.stderr)
		return
	
	client.publish(username + "/admin", "asksschedule", qos=0)

def schedule_makeconsistent(userdata, username, deviceschedule):
	"""Check that the device internal schedule and the database schedule are the same.
	If not, send the necessary messages to the device to fix its schedule (database supersedes).
	"""
	
	cursor      = userdata["cursor"]
	displayname = database.getdisplayname(cursor, username)
	
	if displayname is None:
		print("can't find username " + shlex.quote(str(username)), file=sys.stderr)
		return
	
	dbschedule = database.devschedule(cursor, displayname)
	parsed     = _parseschedule(deviceschedule)
	
	if parsed is None:
		print("invalid device schedule descriptor", file=sys.stderr)
		return
	
	deviceschedule, capacity = parsed
	
	diffextra   = [event for event in deviceschedule if not event in dbschedule]
	diffmissing = [event for event in dbschedule     if not event in deviceschedule]
	
	if len(dbschedule) > capacity:
		print("can't fix device: database schedule event count (" + str(len(dbschedule)) + ") " +
		"is bigger than the maximum schedule memory of the device (" + str(capacity) + ")")
		return
	
	# note that extras must be removed before adding missing events
	# otherwise we may exceed the maximum schedule size for the device
	
	for event in diffextra:
		unschedule(userdata, displayname, event)
	
	for event in diffmissing:
		schedule(userdata, displayname, event)

def _parseschedule(text):
	"""Parse a schedule descriptor (coming from the device) into an appropriate list.
	
	Returns:
		(events, capacity) events.   A list of event object descriptors.
		                   capacity. The max number of scheduled events in this device.
	"""
	
	lines = text.split("\n")
	
	if len(lines) < 1:
		return None
	
	header = lines[0].split("/")
	
	if len(header) != 2:
		return None
	
	try:
		nevents  = int(header[0])
		capacity = int(header[1])
	except ValueError:
		return None
	
	if nevents != len(lines) - 1:
		return None
	
	events = []
	
	try:
		for line in lines[1:]:
			events.append(Event.from_string(line))
	except ValueError:
		return None
	
	return (events, capacity)
