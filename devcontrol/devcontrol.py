# devcontrol.py
# Home automation device control service
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

"""Home automation device control service."""

import sqlite3
import os, sys, signal, subprocess
import json
import time
import select
import shlex
import logging, logging.handlers
import string
import traceback
import paho.mqtt.client as mqtt

from Event import Event
import database
import device
import mqtthandlers

# Exceptions
# ------------------------------------------------------------------------------

class FormatError(Exception):
	"""Formatting error. An input string couldn't be parsed as it did not conform to its schema."""
	pass

# Signals
# ------------------------------------------------------------------------------

def setsignals():
	"""Set termination signals to gracefully close every resource."""
	signal.signal(signal.SIGINT,  gracefulexit)
	signal.signal(signal.SIGTERM, gracefulexit)
	signal.signal(signal.SIGHUP,  gracefulexit)

def gracefulexit(signalnumber, frame):
	"""Close the database, broker and client, and exit the program."""
	
	sys.exit(0)  # resources will be closed on their corresponding finally blocks

# Main routines
# ------------------------------------------------------------------------------

def processline(userdata, line):
	"""Read a line from stdin and execute the corresponding command."""
	
	def processcmd(command, args, expected_nargs, delegate):
		"""Validate the number of arguments and call a delegate handler."""
		
		if len(args) != expected_nargs:
			raise FormatError("Wrong number of arguments for '" + command + "', expected " +
			                  str(expected_nargs) + " but got " + str(len(args)))
			
		delegate(userdata, *args)
	
	# commands should use double quotes if an argument has spaces
	# in it and escape internal double quotes as necessary
	tokens = shlex.split(line)
	if len(tokens) < 1:
		return
	
	cmd  = tokens[0]
	args = tokens[1:]
	
	# Specialized handlers; they bridge the user facing interface and the internal implementations;
	# see the command dictionary below for more information on each operation
	
	def timed_handler(userdata, *args):
		"""Accomodate the input for a non-recurrent event."""
		fuzzy = False
		
		if args[3] == "exact":
			fuzzy = False
		elif args[3] == "fuzzy":
			fuzzy = True
		else:
			raise FormatError("Expected 'fuzzy' or 'exact' but found '" + args[3] + "' instead.")
		
		event = Event.create_once(args[4], fuzzy, args[2])
		
		if args[0] == "add":
			device.schedule(userdata, args[1], event)
		elif args[0] == "del":
			device.unschedule(userdata, args[1], event)
		else:
			raise FormatError("Expected 'add' or 'del' but found '" + args[0] + "' instead.")
	
	def recurrent_handler(userdata, *args):
		"""Accomodate the input for a recurrent event."""
		fuzzy = False
		
		if args[5] == "exact":
			fuzzy = False
		elif args[5] == "fuzzy":
			fuzzy = True
		else:
			raise FormatError("Expected 'fuzzy' or 'exact' but found '" + args[5] + "' instead.")
		
		event = Event.create_recurrent(args[6], fuzzy, args[2], args[3], args[4])
		
		if args[0] == "add":
			device.schedule(userdata, args[1], event)
		elif args[0] == "del":
			device.unschedule(userdata, args[1], event)
		else:
			raise FormatError("Expected 'add' or 'del' but found '" + args[0] + "' instead.")
	
	def devlist_handler(userdata, *args):
		"""Transform the raw devlist into a human readable list."""
		for (dev, connected) in database.devlist(userdata["cursor"]):
			if dev == "devmaster":
				continue
			
			if connected:
				print(shlex.quote("+" + dev), end=" ")
			else:
				print(shlex.quote("-" + dev), end=" ")
		
		print()
	
	def guestlist_handler(userdata, *args):
		"""Transform the raw guestlist into a human readable list."""
		for guest in userdata["guestlist"]:
			print(shlex.quote(guest), end=" ")
		
		print()
	
	def info_handler(userdata, *args):
		"""Transform the raw info list into a human readable list."""
		info = database.devinfo(userdata["cursor"], args[0])
		
		if info is None:
			print("can't find user " + args[0])
			return
		
		stype, connected, status = info
		
		print(shlex.quote(("+" if connected else "-") + stype), end=" ")
		print(shlex.quote(status))
	
	def schedule_handler(userdata, *args):
		"""Transform the raw schedule list into a human readable list."""
		for event in database.devschedule(userdata["cursor"], args[0]):
			print(str(event))
		
		print("")
	
	# Command dictionary
	
	commands = {
		# "command name": (expected_nargs, delegate)
		"add": (4, device.add),
			# add <guestname> <displayname> <type> <status>
			# add a device to the network
		"rename": (2, device.rename),
			# rename <displayname> <newdisplayname>
			# change the public display name of a device in the network
		"del": (1, device.delete),
			# del <displayname>
			# delete a device from the network
		"sync": (1, device.sync),
			# sync <displayname>
			# send a time synchronization message to the specified device
		"ping": (1, device.ping),
			# ping <displayname>
			# check a device is still responsive by sending a ping message
		"askstatus": (1, device.askstatus),
			# askstatus <displayname>
			# ask the device for its current status
		"cmd": (2, device.execute),
			# cmd <displayname> <operation>
			# send immediate command to device;
			# arguments to the operation should be within the operation argument, e.g. 'dimmer 126'
			# the operation must be valid (and have valid arguments) for the device type;
			# otherwise, the command will fail silently
		"timed": (5, timed_handler),
			# timed (add|del) <displayname> <date> (exact|fuzzy) <operation>
			# schedule a command for the future;
			# 'add' indicates to add the operation to the schedule, 'del' indicates to remove it from it;
			# <date> must be formatted as a unix integer timestamp in the future,
			# otherwise the command is a no-op;
			# 'exact' sets the timer for the specific timestamp; 'fuzzy' adds a small amount of time noise;
			# <operation> and <args> follow the same rules as the 'cmd' message;
		"recurrent": (7, recurrent_handler),
			# recurrent (add|del) <displayname> <weekday> <hours> <minutes> (exact|fuzzy) <operation>
			# schedule a recurrent command for the future;
			# 'add' indicates to add the operation to the schedule, 'del' indicates to remove it from it;
			# <weekday> must be a number between 0 and 9. Passing 0 signals the operation should execute
			# every day; 1-7 signal it should be executed on Mon-Sun respectively; 8 signals Mon-Fri; and
			# 9 signals Sat-Sun;
			# 'exact' sets the timer for the specific timestamp; 'fuzzy' adds a small amount of time noise;
			# <operation> and <args> follow the same rules as the 'cmd' message
		"clear": (1, device.clearschedule),
			# clear <displayname>
			# clear the schedule for a given device
		"devlist": (0, devlist_handler),
			# devlist
			# retrieve verified device list
			# respond with a list of devices, using the format: ('(-|+)<displayname>' )*
			# where every name is prepended with a positive sign '+' if the device is connected
			# and a negative sign '-' if it is not
		"guestlist": (0, guestlist_handler),
			# guestlist
			# retrieve the guestlist
			# respond with a list of unverified devices, using the format ('<displayname>' )*
			# note that every guest is connected (otherwise it would just be removed from the list)
		"info": (1, info_handler),
			# info <displayname>
			# retrieve device profile
			# respond with the list of device properties in the profile using the format
			# <connected><type> <status>
			# where connected is formatted as + if the device is connected as - if it is not, e.g.
			# '-sonoff on' indicates a disconnected sonoff device with a status of 'on'
		"schedule": (1, schedule_handler)
			# schedule <displayname>
			# retrieve device schedule
			# respond with a list of scheduled commands for the given device using the formats
			# timed <date> (exact|fuzzy) <operation> <args>
			# recurrent <weekday> <hours> <minutes> (exact|fuzzy) <operation> [<args>]
			# for timed and recurrent operations respectively;
			# the specifics of each formats are the same as for their schedule counterparts
	}
	
	try:
		(expected_nargs, delegate) = commands[cmd]
	except KeyError:
		print("Unrecognized command, skipping", file=sys.stderr)
		return
	
	processcmd(cmd, args, expected_nargs, delegate)

def genconfig(infilename, definitions, outfilename):
	"""Generate an appropriate Mosquitto configuration file."""
	
	with open(infilename, "r") as infile:
		text = infile.read()
	
	template = string.Template(text)
	text     = template.safe_substitute(definitions)
	
	with open(outfilename, "w") as outfile:
		outfile.write(text)

def loop(mosquitto, configuration, db, cursor, guestlist, logger):
	"""Main MQTT/REST API event loop."""
	
	userdata = {"done": False, "database": db, "cursor": cursor,
	            "configuration": configuration, "client": None, "guestlist": guestlist}
	client   = None
	
	try:
		client = mqtt.Client(userdata=userdata)
		client.username_pw_set(configuration["superuser"], configuration["superpass"])
		client.on_connect    = mqtthandlers.onconnect
		client.on_disconnect = mqtthandlers.ondisconnect
		client.on_message    = mqtthandlers.onmessage
		userdata["client"]   = client
		
		client.tls_set(configuration["certificate"])
		client.connect(configuration["devhostname"], configuration["devmqttport"], keepalive=300)
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
						db.commit()
					except FormatError as e:
						print("Error processing line: " + str(e), file=sys.stderr)
					except EOFError:
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

def main():
	"""Program entry point."""
	setsignals()
	
	configuration  = None
	guestlist      = set()
	configfilename = sys.argv[1] if len(sys.argv) > 1 else "configuration.json"
	
	guestlist.add("pipe")
	
	scriptdir = os.path.dirname(os.path.realpath(__file__))
	
	try:
		with open(configfilename) as configfile:
			configuration = json.loads(configfile.read())
	except IOError:
		print("Can't open configuration file", file=sys.stderr)
		return
	
	# the relative path is necessary for the mosquitto configuration file
	# the join adds a trailing os-specific dir separator
	configuration["relpath"] = os.path.join(os.path.relpath("./", scriptdir), "")
	
	with sqlite3.connect(configuration["devdbfile"]) as db:
		cursor = db.cursor()
		
		try:
			database.setupdb(cursor)
			database.setsuperuser(cursor, configuration["superuser"], configuration["superpass"])
		except KeyError:
			print("Incomplete configuration file", file=sys.stderr)
			return
		except sqlite3.Error:
			print("Can't set up the database and super user", file=sys.stderr)
			return
		
		db.commit()
		
		genconfig(os.path.join(scriptdir, "mosquitto.conf.in"), configuration,
		          os.path.join(scriptdir, "mosquitto.conf"))
		
		with subprocess.Popen(["mosquitto", "-c", os.path.join(scriptdir, "mosquitto.conf")],
		                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
		                      stdin=subprocess.DEVNULL, cwd=scriptdir) as mosquitto:
			time.sleep(0.5)  # let mosquitto broker start up
			
			handler = None
			
			try:
				logger  = logging.getLogger(__name__)
				handler = logging.handlers.RotatingFileHandler(os.path.join(scriptdir, "mosquitto.log"),
				                                               "a", 1024 * 1024, 10)
				handler.setFormatter(logging.Formatter())
				logger.setLevel(logging.DEBUG)
				logger.addHandler(handler)
				
				print("MQTT broker started", file=sys.stderr)
				
				loop(mosquitto, configuration, db, cursor, guestlist, logger)
			finally:
				mosquitto.terminate()
				
				if handler is not None:
					remaining = mosquitto.stderr.read()
					logger.debug(remaining.strip().decode("utf8"))
					handler.close()

if __name__ == "__main__":
	main()
