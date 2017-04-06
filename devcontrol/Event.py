# Event.py
# Schedule item definitions
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

"""Schedule item definitions."""

import shlex

# This class could very well be split in two: RecurrentEvent and NonRecurrentEvent, but the whole
# thing is so simple (and both databases and the text device interface don't understand hierarchy
# directly) that it really does not warrant it;
# if in the future it were to get more complex, you should do a proper taxonomy
class Event(object):
	"""Schedule item. Represents a command to be executed in the future."""
	
	def __init__(self, command, fuzzy=False, recurrent=False, firedate=None,
	             weekday=None, hours=None, minutes=None):
		"""Create a new event."""
		if recurrent:
			if weekday is None or hours is None or minutes is None:
				raise ValueError("Weekday, hours and minutes must be set in a recurrent event")
		else:
			if firedate is None:
				raise ValueError("Firedate must be set in a non-recurrent event")
		
		if firedate is not None and (weekday is not None or hours is not None or minutes is not None):
			raise ValueError("Can't give both a firedate and a recurrent specification")
		
		self.command   = str(command)
		self.fuzzy     = bool(fuzzy)
		self.recurrent = bool(recurrent)
		self.firedate  = int(firedate) if firedate is not None else 0
		self.weekday   = int(weekday)  if weekday  is not None else 0
		self.hours     = int(hours)    if hours    is not None else 0
		self.minutes   = int(minutes)  if minutes  is not None else 0
		
		if recurrent:
			if not (0 <= self.weekday <= 9 and 0 <= self.hours <= 23 and 0 <= self.minutes <= 59):
				raise ValueError("Incorrect recurrent schedule specification")
		else:
			if self.firedate < 0:
				raise ValueError("Firedate must be positive")
	
	@staticmethod
	def create_once(command, fuzzy=False, firedate=0):
		"""Create a new non-recurrent event."""
		return Event(command, fuzzy=fuzzy, recurrent=False, firedate=firedate,
		             weekday=None, hours=None, minutes=None)
	
	@staticmethod
	def create_recurrent(command, fuzzy=False, weekday=0, hours=0, minutes=0):
		"""Create a new recurrent event."""
		return Event(command, fuzzy=fuzzy, recurrent=True, firedate=None,
		             weekday=weekday, hours=hours, minutes=minutes)
	
	@staticmethod
	def from_string(line):
		"""Create a new event from a string description.
		
		The string must follow the following format:
		For non-recurrent event, "timed (x|z) EpochTime Command", where
		    the second char (x|z) indicates exact timer or fuzzy match (adds 16-minutes uniform noise)
		    EpochTime is the number of seconds since 1 Jan 1970, 00:00:00
		
		For recurrent events, "recurrent (x|z)(0-9) Hour.Minutes Command", where
		    the second char (x|z) indicates exact timer or fuzzy match (adds 16-minutes uniform noise)
		    the third char indicates a day of the week Mon-Sun (1-7), every day (0),
		    every weekday Mon-Fri (8) or weekends Sat-Sun (9)
		    Hour indicates the hour in 24-hour format using a leading zero if necessary
		    Minutes indicates the minutes using a leading zero if necessary
		"""
		
		words = line.split(" ", 3)
		
		if len(words) != 4:
			raise ValueError("Incorrect number of words in event descriptor")
		
		if words[0] == "timed":
			recurrent = False
			fuzzytext = words[1]
			firedate  = words[2]
			
		elif words[0] == "recurrent":
			if len(words[1]) != 2:
				raise ValueError("Malformed fuzzy-weekday descriptor")
			
			recurrent = True
			fuzzytext = words[1][0]
			weekday   = words[1][1]
			timecomp  = words[2].split(".")
			
			if len(timecomp) != 2:
				raise ValueError("Malformed hour-minute descriptor")
			
			hours   = timecomp[0]
			minutes = timecomp[1]
		else:
			raise ValueError("Unrecognized event type '" + words[0] + "'")
		
		if fuzzytext == "x":
			fuzzy = False
		elif fuzzytext == "z":
			fuzzy= True
		else:
			raise ValueError("Unrecognized fuzzy descriptor")
		
		command = words[3]
		
		if recurrent:
			return Event.create_once(command, fuzzy, firedate)
		else:
			return Event.create_recurrent(command, fuzzy, weekday, hours, minutes)
	
	def __str__(self):
		"""Represent this event as string. The format is the same as the from_string method."""
		fztext = "z" if self.fuzzy else "x"
		
		if self.recurrent:
			return "recurrent {}{} {:02d}.{:02d} {}".format(fztext, self.weekday, self.hours,
			                                                self.minutes, shlex.quote(self.command))
		else:
			return "timed {} {} {}".format(fztext, self.firedate, shlex.quote(self.command))
