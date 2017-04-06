# sensors.py
# Sensor definitions
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

"""Sensor definitions."""

# this dictionary defines the acceptable sensor types, their acceptable commands
# and the status each command leaves the sensor in;
#
# the command list or the status dict may be switch with lambda functions
# if more expressibility is required.
# In such case, the commands function commands(command, status) = True if command is valid 
# when the device in the given status, else False;
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
