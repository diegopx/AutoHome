// config.h
// Configuration constants
// Part of AutoHome
//
// Copyright (c) 2017, Diego Guerrero
// All rights reserved.
// 
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are met:
//     * Redistributions of source code must retain the above copyright
//       notice, this list of conditions and the following disclaimer.
//     * Redistributions in binary form must reproduce the above copyright
//       notice, this list of conditions and the following disclaimer in the
//       documentation and/or other materials provided with the distribution.
//     * The names of its contributors may not be used to endorse or promote products
//       derived from this software without specific prior written permission.
// 
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
// ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
// WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
// DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS BE LIABLE FOR ANY
// DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
// (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
// LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
// ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
// (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
// SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

#ifndef CONFIG_H
#define CONFIG_H

// These strings must fit in a buffer of size 'maxcfgstrsize'
#define DEFAULT_WIFI_SSID "default-ssid"
#define DEFAULT_WIFI_PASS "default-pass"

// Hard-coded settings
const int  version           = 1;  // firmware version
const char masterhost   [20] = "autohome.local";
const int  masterporthttps   = 443;
const int  masterportmqtt    = 8883;
const char firmwareuri  [44] = "/static/sonoff-firmware.bin";
const char accessuri    [28] = "/static/access";
const char authorization[32] = "guest-secret";
const char fingerprint  [60] = "11 22 33 44 55 66 77 88 99 00 AA SS CC DD EE FF 11 22 33 44";
const char mqtt_prefix  [10] = "sonoff-";
const int  mqtt_maxattempts  = 24;  // after this many attempts to reconnect, reset device
const int  maxcfgstrsize     = 44;  // max string length for usernames and passwords considering
                                    // the null terminator; should be a multiple of 4
const int  maxnscheduled     = 32;  // max number of scheduled commands (trying to add another one
                                    // will silently fail); should be a multiple of 4

#endif  // #ifndef CONFIG_H
