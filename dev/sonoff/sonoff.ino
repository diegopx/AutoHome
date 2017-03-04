// sonoff.ino
// Wireless switch firmware
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

#include <ESP8266WiFi.h>

#include <stdlib.h>
#include <ctype.h>
#include <limits.h>

#include <EEPROM.h>
#include <Ticker.h>
#include <ESP8266mDNS.h>
#include <ESP8266httpUpdate.h>
#include <PubSubClient.h>
#include <lwip/ip_addr.h>
#include <lwip/err.h>
#include <lwip/dns.h>

#include "config.h"

#define DEBUG_SETTINGS 1

#define BTN_PRESSED    LOW
#define BTN_NOTPRESSED HIGH
#define LED_ON         LOW
#define LED_OFF        HIGH
#define RELAY_ON       HIGH
#define RELAY_OFF      LOW

const int buttonpin = 0;
const int relaypin  = 12;
const int ledpin    = 13;
const int freepin   = 14;

// Command description to be run at a later time
typedef struct ScheduledCmd
{
	char     command   = '0';    // '0': turn off, '1': turn on
	byte     reserved1 = 0;
	bool     fuzzy     = false;  // if true, execute the command at a random time in a 16 minute window around the set time
	bool     recurrent = false;  // trigger this command recurrently every week
	byte     weekday   = 0;      // 1-7: trigger on Mon, ..., Sun; 0: every day; 8: every weekday Mon-Fri, 9: every weekend Sat-Sun
	byte     hours     = 0;      // trigger at this hour
	byte     minutes   = 0;      // and this minutes
	byte     reserved2 = 0;
	uint64_t firedate  = 0;      // one-off command date
} ScheduledCmd;

// Non-volatile settings saved in the EEPROM portion of the flash memory
typedef struct Settings
{
	uint32_t     checksum                 = 0;  // crc32 checksum
	char         ssid     [maxcfgstrsize] = DEFAULT_WIFI_SSID;
	char         password [maxcfgstrsize] = DEFAULT_WIFI_PASS;
	char         mqtt_user[maxcfgstrsize] = "";
	char         mqtt_pass[maxcfgstrsize] = "";
	int          nscheduled               = 0;  // number of active scheduled commands
	ScheduledCmd schedule[maxnscheduled];       // scheduled commands
} Settings;

Settings         settings;
IPAddress        masterip;
WiFiClientSecure wifi;
PubSubClient     mqtt(wifi, fingerprint);
uint32_t         lastmillis;
uint64_t         curdate;  // number of seconds since 1970 (reset after around 5 * 10^11 years)
                           // do not assume date always increases, it will be constantly
                           // corrected by timestamps from the server

// Time of the next time the corresponding command will be executed;
// separated from ScheduledCmd because this changes constantly and
// so it's better to keep it out of EEPROM (to reduce the number of
// consistency checks and writes to flash memory)
uint64_t nextfire[maxnscheduled];

// Auxiliar functions
// ------------------------------------------------------------------------------

// calculate crc32 checksum (to validate hardware integrity, do not use for security)
uint32_t crc32(void* data, int size)
{
	uint32_t checksum = 0xffffffff;
	uint8_t* begin    = static_cast<uint8_t*>(data);
	uint8_t* end      = begin + size;
	
	for (uint8_t* i = begin; i < end; i++) {
		for (uint32_t k = 0x80; k > 0; k >>= 1) {
			bool bit = checksum & 0x80000000;
			
			checksum <<= 1;
			bit       ^= (*i & k);
			
			if (bit) {
				checksum ^= 0x04c11db7;
			}
		}
	}
	
	return checksum;
}

// calculate the checksum for a given settings struct removing the effect
// of the checksum field itself to avoid a cyclic dependence
uint32_t settings_checksum(Settings* settings)
{
	uint32_t old_checksum = settings->checksum;
	settings->checksum    = 0;
	
	uint32_t checksum = crc32(settings, sizeof (Settings));
	
	settings->checksum = old_checksum;
	
	return checksum;
}

// print a detailed description of the global settings
void dump_settings()
{
	int i;
	Serial.printf("dumping settings (size = %d):\r\n", sizeof(settings));
	Serial.println();
	
	char* raw = (char*) &settings;
	
	for (i = 0; i < sizeof (settings); i++) {
		Serial.printf("%02x", raw[i]);
		
		if (i % (2 * 8) == (2 * 8 - 1)) {
			Serial.print("\r\n");
		}
		else if (i % 2 == 1) {
			Serial.print(" ");
		}
	}
	
	Serial.println();
	Serial.println();
	
	Serial.printf("checksum: %d | 0x%x\r\n", settings.checksum, settings.checksum);
	Serial.printf("ssid: '%s'; pass: '%s'\r\n", settings.ssid, settings.password);
	Serial.printf("mqtt user: '%s'; mqtt pass: '%s'\r\n", settings.mqtt_user, settings.mqtt_pass);
	Serial.printf("nscheduled: %d\r\n", settings.nscheduled);
	Serial.println();
	
	for (i = 0; i < settings.nscheduled; i++) {
		Serial.printf("schedule[%d]: cmd: %c; fuzzy: %d; recurrent: %d, firedate: %d\r\n", i,
		              settings.schedule[i].command, settings.schedule[i].fuzzy,
		              settings.schedule[i].recurrent, (int) settings.schedule[i].firedate);
	}
}

// write settings to EEPROM
void flush_settings()
{
	settings.checksum = settings_checksum(&settings);
	
#if DEBUG_SETTINGS
	Serial.println("new settings");
	dump_settings();
#endif
	
	EEPROM.begin(sizeof (Settings));
	EEPROM.put(0, settings);
	EEPROM.end();
}

// read an unsigned 64 bit integer from a string, similar to strtoull,
// which is not implemented in the SDK or the libraries, raising a linker error
uint64_t readull(const char* str, const char** stop)
{
	uint64_t    value = 0;
	const char* c;
	
	for (c = str; *c != 0; c++) {
		int digit = *c - '0';
		
		if (digit < 0 || 9 < digit) {
			break;
		}
		
		uint64_t maxacceptable = (ULONG_LONG_MAX - digit) / 10;
		
		if (value > maxacceptable) {
			break;
		}
		
		value *= 10;
		value += digit;
	}
	
	if (stop != nullptr) {
		*stop = c;
	}
	
	return value;
}

// day of the week where 1 = Monday ... 7 = Sunday
byte weekday(uint64_t time)
{
	// Jan 01 1970 (time == 0) was a Thursday, i.e. weekday(0) = 4
	// every unix day is exactly 60 * 60 * 24 = 86400 seconds (leap seconds are discarded)
	return ((time / 86400 + 3) % 7) + 1;
}

// seconds since the previous midnight
int midnightseconds(uint64_t time)
{
	return time % 86400;
}

// hours since the previous midnight
int hours(uint64_t time)
{
	return (time % 86400) / 3600;
}

// minutes since the previous hour
int minutes(uint64_t time)
{
	return (time % 3600) / 60;
}

// seconds since the previous minute
int seconds(uint64_t time)
{
	return time % 60;
}

// LED interface
// ------------------------------------------------------------------------------

int    blinkiter  = 0;  // to keep track of times the led has been switched
int    blinkcount = 0;
Ticker blinkticker;

// blink the led a specified number of times (blocking)
void ledblink(int count)
{
	for (int i = 0; i < count; i++) {
		digitalWrite(ledpin, LED_ON);
		delay(400);
		digitalWrite(ledpin, LED_OFF);
		delay(400);
	}
}

// blink the led a specified number of times (asynchronous)
// if count <= 0, will blink indefinitely until stopledblink() is called
void asyncledblink(int count)
{
	blinkiter  = 0;
	
	if (count > 0) {
		blinkcount = 2 * count;  // one call per low->high, one per high->low
	}
	else {
		blinkcount = INT_MAX;  // count <= 0: never stop 
	}
	
	digitalWrite(ledpin, LED_OFF);
	asyncledblink0();  // start immediately, do not wait for the first timer
	
	blinkticker.attach(0.4, asyncledblink0);
}

// internal asynchronous led blinking callback
void asyncledblink0()
{
	if (blinkiter < blinkcount) {
		digitalWrite(ledpin, !digitalRead(ledpin));
		blinkiter += 1;
	}
	else {
		stopledblink();
	}
}

// stop the led from blinking
void stopledblink()
{
	blinkticker.detach();
}

// Reset
// ------------------------------------------------------------------------------

// signal the user and restart the processor;
// the user may reset the settings to their default values
// by pressing the button before the LED stops blinking
// and keeping it pressed for 10 seconds
void restart()
{
	Serial.print("Restarting");
	
	digitalWrite(ledpin, LED_ON);  delay(150);
	digitalWrite(ledpin, LED_OFF); delay(150);
	digitalWrite(ledpin, LED_ON);  delay(200);
	digitalWrite(ledpin, LED_OFF); delay(300);
	
	Serial.print(".");
	
	digitalWrite(ledpin, LED_ON);  delay(800);
	digitalWrite(ledpin, LED_OFF); delay(600);
	
	Serial.print(".");
	
	digitalWrite(ledpin, LED_ON);  delay(300);
	digitalWrite(ledpin, LED_OFF); delay(150);
	digitalWrite(ledpin, LED_ON);  delay(150);
	digitalWrite(ledpin, LED_OFF); delay(200);
	
	Serial.println(".");
	
	if (digitalRead(buttonpin) == BTN_PRESSED) {
		delay(4000);
		
		if (digitalRead(buttonpin) == BTN_PRESSED) {
			resetconfig();
			
			digitalWrite(ledpin, LED_ON);  delay(200);
			digitalWrite(ledpin, LED_OFF); delay(200);
			digitalWrite(ledpin, LED_ON);  delay(200);
			digitalWrite(ledpin, LED_OFF); delay(200);
			digitalWrite(ledpin, LED_ON);  delay(200);
			digitalWrite(ledpin, LED_OFF); delay(500);
		}
	}
	
	ESP.restart();
}

// resets configuration to its factory settings
void resetconfig()
{
	Serial.println("Resetting configuration");
	WiFi.disconnect();
	
	Settings settings = Settings();
	flush_settings();
}

// Scheduler
// ------------------------------------------------------------------------------

Ticker      sticker;
const char* report_status;              // if not null, indicates a status response should be sent and
                                        // the status line corresponds to this string

// update the value of curdate;
// should be called at least every 49 days (otherwise it would skip an millis() overflow)
void updatetime()
{
	uint32_t mil = millis();                                   // note that just using date += (millis() - lastmillis) / 1000
	curdate     += (mil - 1000 * (lastmillis / 1000)) / 1000;  // would lose accuracy due to roundoff errors, so we first round lastmillis
	lastmillis   = mil;                                        // to the second, i.e. add the same amount of milliseconds that were
	                                                           // rounded off in the previous iteration
	
	Serial.printf("now: %d\r\n", (int) curdate);
}

// compare two ScheduledCmd
// return value == 0 if equal, value < 0 if a < b, value > 0 if a > b
// order defined by (day, hour, minutes, command, recurrent, fuzzy)
int scommandcmp(const ScheduledCmd& a, const ScheduledCmd& b)
{
	int diff = 0;
	
	return ((diff = a.weekday   - b.weekday)   != 0) ? diff :
	       ((diff = a.hours     - b.hours)     != 0) ? diff :
	       ((diff = a.minutes   - b.minutes)   != 0) ? diff :
	       ((diff = a.command   - b.command)   != 0) ? diff :
	       ((diff = a.recurrent - b.recurrent) != 0) ? diff :
	       (        a.fuzzy     - b.fuzzy);
}

// linear search on the haystack
// return the index of the matched command or -1 if it was not found
// avoid sort + binary search as it implies unnecessary writes to EEPROM
// and given the small schedule size and the sparsity of update events it is not worth it
int findscommand(const ScheduledCmd& needle, ScheduledCmd* haystack, int hsize)
{
	Serial.println("finding command");
	Serial.printf("needle: cmd: %c; fuzzy: %d; recurrent: %d, firedate: %d\r\n", needle.command, needle.fuzzy, needle.recurrent, (int) needle.firedate);
	
	for (int i = 0; i < hsize; i++) {
		if (scommandcmp(needle, haystack[i]) == 0) {
			Serial.println("found it");
			return i;
		}
	}
	
	return -1;
}

// add a new scheduled command
// if the same command is already in the schedule,
// or if no there is no more room in the scheduler array, do nothing and return false
bool schedulecommand(const ScheduledCmd& command)
{
	Serial.println("scheduling command");
	Serial.printf("command: cmd: %c; fuzzy: %d; recurrent: %d, firedate: %d\r\n", command.command, command.fuzzy, command.recurrent, (int) command.firedate);
	
	if (settings.nscheduled >= maxnscheduled) {
		return false;
	}
	
	int index = findscommand(command, settings.schedule, settings.nscheduled);
	
	if (index >= 0) {
		return false;
	}
	
	settings.schedule[settings.nscheduled] = command;
	calculatenextfire(settings.nscheduled++);
	updatescallback();
	
	flush_settings();
	
	return true;
}

// remove a scheduled command from the schedule
// if it was not found, do nothing and return false
bool unschedulecommand(const ScheduledCmd& command)
{
	Serial.println("unscheduling command");
	Serial.printf("command: cmd: %c; fuzzy: %d; recurrent: %d, firedate: %d\r\n", command.command, command.fuzzy, command.recurrent, (int) command.firedate);
	
	int index = findscommand(command, settings.schedule, settings.nscheduled);
	
	if (index < 0) {
		return false;
	}
	
	settings.schedule[index] = settings.schedule[settings.nscheduled];
	nextfire         [index] = nextfire         [settings.nscheduled--];
	
	flush_settings();
	
	return true;
}

// calculate the nextfire property for every scheduled command
void calculatenextfire()
{
	Serial.printf("recalculating all firedates (n = %d)\r\n", settings.nscheduled);
	
	for (int i = 0; i < settings.nscheduled; i++) {
		calculatenextfire(i);
	}
}

// calculate the nextfire property for the scheduled command at index i
// if the index is out-of-bounds, do nothing and return false
bool calculatenextfire(int i)
{
	Serial.printf("recalculating firedate for schedule[%d]\r\n", i);
	Serial.printf("schedule[%d]: cmd: %c; fuzzy: %d; recurrent: %d, firedate: %d\r\n", i,
	              settings.schedule[i].command, settings.schedule[i].fuzzy,
	              settings.schedule[i].recurrent, (int) settings.schedule[i].firedate);
	
	if (i < 0 || i >= maxnscheduled) {
		return false;
	}
	
	if (!settings.schedule[i].recurrent) {
		nextfire[i] = settings.schedule[i].firedate;
	}
	else {
		updatetime();
		
		uint32_t wday     = weekday(curdate) % 7;
		uint32_t secs     = midnightseconds(curdate);
		uint64_t midnight = curdate - secs;
		
		uint32_t schwday = settings.schedule[i].weekday;  // schedule weekday and seconds since midnight
		uint32_t schsecs = 60 *(settings.schedule[i].minutes + 60 * settings.schedule[i].hours);
		
		if (schsecs <= secs) {           // at an earlier time (so it must start counting from tomorrow)
			midnight += 24 * 60 * 60;    // the equality forbids setting a recurrent firedate for right now (only for next week)
			wday      = (wday + 1) % 7;  // to avoid re-raising an event multiple times when rescheduling after the event handler;
		}                                // in practice nobody should rely on a 1-second-precision based decision anyway
		
		// note that at least one of the possible weekdays must satisfy the conditional
		for (int k = 0; k < 7; k++) {
			if (schwday == 0 ||                 // the schedule is every day, so today too
			    schwday == wday ||              // the schedule is for today, (note the differences between 0-index and 1-index
			    (schwday == 7 && wday == 0) ||  // and also between starting in Sunday and Monday)
			    (schwday == 8 && 1 <= wday && wday <= 5) ||    // the schedule is for weekday and today is a weekday
			    (schwday == 9 && (wday == 0 || wday == 6))) {  // the schedule is for weekend and today is a weekend
				
				nextfire[i] = midnight + schsecs;
				Serial.printf("found match: %d, %d; new firedate: %d\r\n", k, wday, (int) nextfire[i]);
				break;
			}
			
			midnight += 24 * 60 * 60;
			wday      = (wday + 1) % 7;
		}
	}
	
	if (settings.schedule[i].fuzzy) {  // add noise
		const int halfnoise = 8 * 60;  // noise will be +-8 mins, i.e. a total spread of 16 minutes
		int lowbound  = (nextfire[i] > halfnoise)                  ? 0 : halfnoise - nextfire[i];
		int highbound = (nextfire[i] < ULONG_LONG_MAX - halfnoise) ? 2 * halfnoise : ULONG_LONG_MAX + halfnoise - nextfire[i];
		
		nextfire[i] += random(lowbound, highbound) - halfnoise;
		
		Serial.printf("fuzzy; lowbound: %d; highbound: %d; new firedate: %d\r\n", lowbound - halfnoise, highbound - halfnoise, (int) nextfire[i]);
	}
	
	return true;
}

// update callback timeout according to the soonest fire
void updatescallback()
{
	Serial.println("recalculating callback timeout");
	if (settings.nscheduled == 0) {
		sticker.once_ms((uint32_t) ULONG_MAX, scallback);  // even if there are no events, calling the callback will
		return;                                            // force a time update, so we don't miss a millis() overflow
	}
	
	updatetime();
	uint64_t next = ULONG_LONG_MAX;
	
	for (int i = 0; i < settings.nscheduled; i++) {
		next = (nextfire[i] < next) ? nextfire[i] : next;
	}
	
	next = (next > curdate) ? next : curdate;  // limit to the present or future, not the past;
	                                           // if there's a command in the past, the callback will be called immediately
	
	uint64_t diff = next - curdate;
	
	diff = (diff < ULONG_MAX / 1000) ? diff : ULONG_MAX / 1000;  // if it is too far into the future (ticker uses 32 bits
	                                                             // for the timestamp), just set the callback as far
	                                                             // as possible; the callback will do nothing but set
	                                                             // the next callback until diff is small enough
	
	Serial.printf("new timeout: %d\r\n", (int) diff);
	
	sticker.detach();
	sticker.once_ms((uint32_t) (1000 * diff), scallback);
}

// scheduler callback, usually called when there is a scheduled command to be run
// check every scheduled command and run it if its fire date is within 5 seconds of right now
// then remove it if it's a one-off, or set its fire date to its following date
// if an old command (> 5 sec) is found, ignore it, it's too late now
void scallback()
{
	// if more than one command must be executed, only execute the last one
	// NOTE this relies on the particular commands of the switch, 'on' and 'off',
	//      which are absorbent, i.e. the result of applying a sequence of commands
	//      (instantaneously) is the same as applying the last one
	uint64_t lastexectime = 0;
	byte     lastexeccmd  = 0;
	
	updatetime();
	
	Serial.printf("checking for actions to be performed, date: %d; n = %d\r\n", (int) curdate, settings.nscheduled);
	
	for (int i = 0; i < settings.nscheduled; i++) {
		uint64_t exectime = nextfire[i];
		
		Serial.printf("checking nextfire[%d]: %d\r\n", i, (int) exectime);
		
		if (exectime > curdate + 5) {  // not yet
			Serial.println("  in the future, skip");
			continue;
		}
		
		if ((!settings.schedule[i].fuzzy && exectime  < curdate - 5) ||       // too old
		    ( settings.schedule[i].fuzzy && exectime  < curdate - 8 * 60)) {  // fuzzy gets 8 minutes of grace
			// do not include in execution
			Serial.println("too old, discard");
		}
		else {
			Serial.println("candidate to execute");
			if (exectime > lastexectime) {
				Serial.println("latest so far");
				lastexectime = exectime;
				lastexeccmd  = settings.schedule[i].command;
			}
		}
		
		// reset
		if (settings.schedule[i].recurrent) {
			Serial.println("recurrent: resetting the firedate");
			calculatenextfire(i);
		}
		else {
			Serial.println("one-off: kill it");
			unschedulecommand(settings.schedule[i--]);
		}
	}
	
	
	if (lastexeccmd == '0') {
		Serial.println("relay -> off");
		report_status = "off";
		digitalWrite(relaypin, RELAY_OFF);
	}
	else if (lastexeccmd == '1') {
		Serial.println("relay -> on");
		report_status = "on";
		digitalWrite(relaypin, RELAY_ON);
	}
	
	updatescallback();
}

// MQTT
// ------------------------------------------------------------------------------

char lobbytopic  [maxcfgstrsize + 10];  // cached lobby topic   "<username>/lobby"
char controltopic[maxcfgstrsize + 10];  // cached control topic "<username>/topic"
char admintopic  [maxcfgstrsize + 10];  // cached admin topic   "<username>/admin"
bool mqtt_hascreds;                     // true if the device remembers a user and password for MQTT
bool should_reconnect;                  // true if enough time has pass to reconnect to the MQTT broker
bool should_ping;                       // true if the mqtt client should pingback as soon as possible

// connect to the MQTT server
bool mqtt_connect()
{
	Serial.println("Connecting to MQTT broker");
	
	bool connected = false;
	
	int  usernamelen = strlen(settings.mqtt_user);
	
	strncpy(&lobbytopic[0], settings.mqtt_user, maxcfgstrsize);  // wait for the server to give you credentials;
	strncpy(&lobbytopic[usernamelen], "/lobby", 6);              // it will usually pop a notification to the
	lobbytopic[maxcfgstrsize + 9] = 0;                           // user, who must approve the new device
	
	if (mqtt_hascreds) {
		Serial.printf("Found credentials, connecting as %s\r\n", settings.mqtt_user);
		connected = mqtt.connect(settings.mqtt_user, settings.mqtt_user, settings.mqtt_pass,
		                         lobbytopic, 1, 0, "abruptly disconnected");
	}
	else {  // use guest secret key to prove network access authorization
		Serial.printf("Credentials not found, connecting as %s using the guest secret\r\n", settings.mqtt_user);
		connected = mqtt.connect(settings.mqtt_user, settings.mqtt_user, authorization,
		                         lobbytopic, 1, 0, "abruptly disconnected");
	}
	
	// authentication and authorization
	
	if (!connected) {
		int state = mqtt.state();
		
		Serial.println("Can't connect to MQTT server");
		
		if (state == MQTT_CONNECT_BAD_CLIENT_ID ||
		    state == MQTT_CONNECT_BAD_CREDENTIALS ||
		    state == MQTT_CONNECT_UNAUTHORIZED) {
			if (mqtt_hascreds) {  // faulty credentials, try to enter as a guest next time
				mqtt_hascreds      = false;
				(mqtt_prefix + String(static_cast<unsigned long>(random(INT_MIN, INT_MAX)), HEX)).toCharArray(settings.mqtt_user, maxcfgstrsize);
				
				Serial.println("Faulty MQTT credentials, resetting");
			}
		}
		else if (state == MQTT_TLS_BAD_SERVER_CREDENTIALS) {
			Serial.println("Incorrect MQTT server fingerprint, resetting");
		}
		
		return false;
	}
	
	// subscriptions
	
	Serial.println("Subscribing to channels");
	
	if (mqtt_hascreds) {
		strncpy(admintopic, settings.mqtt_user, maxcfgstrsize);
		strncpy(&admintopic[usernamelen], "/admin", 6);
		admintopic[maxcfgstrsize + 9] = 0;
		
		mqtt.subscribe(admintopic);
		
		strncpy(controltopic, settings.mqtt_user, maxcfgstrsize);
		strncpy(&controltopic[usernamelen], "/control", 8);
		controltopic[maxcfgstrsize + 9] = 0;
		
		mqtt.subscribe(controltopic);
		
		mqtt.publish(lobbytopic, "hello");
		
		Serial.println("Subscribed to admin and control");
	}
	
	mqtt.subscribe(lobbytopic);
	
	Serial.println("Subscribed to lobby");
	
	return true;
}

// ask the server for a username and a password
// the server will in turn ask the final user through a notification
// whether to accept the new device into his network
void askcredentials()
{
	char message[] = "credentials please";
	mqtt.publish(lobbytopic, message);
}

// read one char and compare to two acceptable options (mapped to true and false)
// set an output argument to the appropriate value
// if the char does not correspond to neither option, return false
bool readbool(char c, char truevalue, char falsevalue, bool& output)
{
	if (c == truevalue) {
		output = true;
	}
	else if (c == falsevalue) {
		output = false;
	}
	else {
		return false;
	}
	
	return true;
}

// process received message from the MQTT network
void mqtt_receive(char* topic, byte* payload, unsigned int length)
{
	char*        data        = reinterpret_cast<char*>(payload);
	unsigned int usernamelen = strlen(settings.mqtt_user);
	
	Serial.println("Processing message:");
	
	for (unsigned int i = 0; i < length; i++) {
		Serial.print(data[i]);
	}
	
	Serial.println();
	
	int tlen = strlen(topic);
	
	if (tlen <= usernamelen + 2) {  // too short, it has to be at least '<username>/x' long
		Serial.printf("Received message from incorrect topic: %s\r\n", topic);
		return;
	}
	
	unsigned int i;
	for (i = 0; i < usernamelen; i++) {  // incorrect prefix, should be '<username>/'
		if (topic[i] != settings.mqtt_user[i]) {
			Serial.printf("Received message from incorrect topic: %s\r\n", topic);
			return;
		}
	}
	
	if (topic[i] != '/') {
		Serial.printf("Received message from incorrect topic: %s\r\n", topic);
		return;
	}
	
	char*        channel = &topic[usernamelen + 1];
	unsigned int clen    = tlen - usernamelen - 1;
	
	if (mqtt_hascreds) {
		if (clen == 7 && strncmp("control", channel, 7) == 0) {  // topic == <username>/control
			Serial.println("Control message...");
			
			if (length == 2 && strncmp("on", data, 2) == 0) {
				Serial.println("relay -> on");
				digitalWrite(relaypin, RELAY_ON);
			}
			else if (length == 3 && strncmp("off", data, 3) == 0) {
				Serial.println("relay -> off");
				digitalWrite(relaypin, RELAY_OFF);
			}
			else if (length == 6 && strncmp("toggle", data, 6) == 0) {
				Serial.print("relay -> toggle (");
				Serial.print(!digitalRead(relaypin) ? "on" : "off");
				Serial.println(")");
				digitalWrite(relaypin, !digitalRead(relaypin));
			}
			else if (length == 5 && strncmp("clear", data, 5) == 0) {
				Serial.println("Clearing the schedule");
				
				settings.nscheduled = 0;
				flush_settings();
			}
			else if (length > 5 && strncmp("timed", data, 5) == 0) {  // set new pre-programmed switch
				// format timed (-|+)(x|z) EpochTime Command
				// the first char (-|+) indicates if the timer must be added (+) or removed (-)
				// the second char (x|z) indicates exact timer or fuzzy match (adds 16-minutes uniform noise)
				// EpochTime is the number of seconds since 1 Jan 1970, 00:00:00
				// The last argument indicates turning off or on (using the same semantics as the on and off commands)
				// e.g. '+z 1633436220 on' means 'turn the switch on around Oct 5 2021, 09:17:00 (+- 8 minutes)'
				
				bool          add   = true;
				bool          fuzzy = false;
				unsigned int  i     = 5;
				
				Serial.println("One-off event");
				
				if (i >= length || !std::isspace(data[i])) {
					Serial.printf("'Timed' packet: Incorrect format at %d: expected whitespace, found %c\r\n", i, data[i]);
					return;
				}
				
				while (i < length && std::isspace(data[i])) {
					i += 1;
				}
				
				if (i >= length || !readbool(data[i], '+', '-', add)) {
					Serial.printf("'Timed' packet: Incorrect format at %d: expected [-+], found %c\r\n", i, data[i]);
					return;
				}
				
				i += 1;
				
				if (i >= length || !readbool(data[i], 'z', 'x', fuzzy)) {
					Serial.printf("'Timed' packet: Incorrect format at %d: expected [zx], found %c\r\n", i, data[i]);
					return;
				}
				
				i += 1;
				
				if (i >= length || !std::isspace(data[i])) {
					Serial.printf("'Timed' packet: Incorrect format at %d: expected whitespace, found %c\r\n", i, data[i]);
					return;
				}
				
				char buffer[16];  // hopefully we have moved on from relying on C overflowable arrays
				                  // by the time this buffer can't hold the corresponding EpochTime
				
				while (i < length && std::isspace(data[i])) {
					i += 1;
				}
				
				int timelen = length - i;
				
				if (timelen > 15) {
					Serial.println("'Timed' packet: time string too long");
					return;
				}
				
				unsigned int k;
				for (k = 0; i < length && k < 15; k++, i++) {
					if (std::isspace(data[i])) {
						break;
					}
					
					buffer[k] = data[i];
				}
				
				buffer[k] = 0;
				
				const char*  readend;
				uint64_t     time = readull(buffer, &readend);
				ScheduledCmd newcmd;
				
				if (*readend != 0) {
					Serial.println("'Timed' packet: can't read timestamp");
					return;
				}
				
				if (i >= length || !std::isspace(data[i])) {
					Serial.printf("'Timed' packet: Incorrect format at %d: expected whitespace, found %c\r\n", i, data[i]);
					return;
				}
				
				while (i < length && std::isspace(data[i])) {
					i += 1;
				}
				
				int remaining = length - i;
				
				if (remaining == 2 && strncmp("on", &data[i], 2) == 0) {
					newcmd.command = '1';
				}
				else if (remaining == 3 && strncmp("off", &data[i], 3) == 0) {
					newcmd.command = '0';
				}
				else {
					Serial.printf("'Timed' packet: Incorrect format at %d: expected [(on)(off)], found '", i);
					
					for (int k = 0; k < remaining; k++) {
						Serial.printf("%c", data[i + k]);
					}
					
					Serial.println("'");
					return;
				}
				
				newcmd.fuzzy     = fuzzy;
				newcmd.recurrent = false;
				newcmd.firedate  = time;
				
				if (add) {
					schedulecommand(newcmd);
				}
				else {
					unschedulecommand(newcmd);
				}
			}
			else if (length > 9 && strncmp("recurrent", data, 9) == 0) {  // set new recurrent pre-programmed switch
				// format: recurrent (-|+)(x|z)(0-9) Hour.Minutes Command
				// the first char (-|+) indicates if the timer must be added (+) or removed (-)
				// the second char (x|z) indicates exact timer or fuzzy match (adds 16-minutes uniform noise)
				// the third char indicates a day of the week Mon-Sun (1-7), every day (0),
				// every weekday Mon-Fri (8) or weekends Sat-Sun (9)
				// Hour indicates the hour in 24-hour format using a leading zero if necessary
				// Minutes indicates the minutes using a leading zero if necessary
				// The last argument indicates turning off or on (using the same semantics as the on and off commands)
				// e.g. '+x6 16.51 off' means 'turn the switch off every Saturday at 16:51'
				
				bool          add     = true;
				bool          fuzzy   = false;
				byte          weekday = 0;
				byte          hours   = 0;
				byte          minutes = 0;
				unsigned int  i       = 9;
				
				Serial.println("Recurrent event");
				
				if (i >= length || !std::isspace(data[i])) {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected whitespace, found %c\r\n", i, data[i]);
					return;
				}
				
				while (i < length && std::isspace(data[i])) {
					i += 1;
				}
				
				if (i >= length || !readbool(data[i], '+', '-', add)) {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected [-+], found %c\r\n", i, data[i]);
					return;
				}
				
				i += 1;
				
				if (i >= length || !readbool(data[i], 'z', 'x', fuzzy)) {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected [zx], found %c\r\n", i, data[i]);
					return;
				}
				
				i += 1;
				
				if (i < length && '0' <= data[i] && data[i] <= '9') {
					weekday = payload[i] - '0';
					i += 1;
				}
				else {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected digit, found %c\r\n", i, data[i]);
					return;
				}
				
				if (i >= length || !std::isspace(data[i])) {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected whitespace, found %c\r\n", i, data[i]);
					return;
				}
				
				while (i < length && std::isspace(data[i])) {
					i += 1;
				}
				
				if (i + 1 < length &&
				    '0' <= data[i]     && data[i]     <= '9' &&
				    '0' <= data[i + 1] && data[i + 1] <= '9') {
					
					hours = 10 * (data[i] - '0') + (data[i + 1] - '0');
					i += 2;
				}
				else {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected hours, found %c%c\r\n", i, data[i], data[i + 1]);
					return;
				}
				
				if (data[i] != '.') {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected '.', found %c\r\n", i, data[i]);
					return;
				}
				
				i += 1;
				
				if (i + 1 < length &&
				    '0' <= data[i]     && data[i]     <= '9' &&
				    '0' <= data[i + 1] && data[i + 1] <= '9') {
					
					minutes = 10 * (data[i] - '0') + (data[i + 1] - '0');
					i += 2;
				}
				else {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected minutes, found %c%c\r\n", i, data[i], data[i + 1]);
					return;
				}
				
				ScheduledCmd newcmd;
				
				if (i >= length || !std::isspace(data[i])) {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected whitespace, found %c\r\n", i, data[i]);
					return;
				}
				
				while (i < length && std::isspace(data[i])) {
					i += 1;
				}
				
				int remaining = length - i;
				
				if (remaining == 2 && strncmp("on", &data[i], 2) == 0) {
					newcmd.command = '1';
				}
				else if (remaining == 3 && strncmp("off", &data[i], 3) == 0) {
					newcmd.command = '0';
				}
				else {
					Serial.printf("'Recurrent' packet: Incorrect format at %d: expected [(on)(off)], found '", i);
					
					for (int k = 0; k < remaining; k++) {
						Serial.printf("%c", data[i + k]);
					}
					
					Serial.println("'");
					return;
				}
				
				newcmd.fuzzy     = fuzzy;
				newcmd.recurrent = true;
				newcmd.weekday   = weekday;
				newcmd.hours     = hours;
				newcmd.minutes   = minutes;
				
				if (add) {
					schedulecommand(newcmd);
				}
				else {
					unschedulecommand(newcmd);
				}
			}
		}
		else if (clen == 5 && strncmp("admin", channel, 5) == 0) {  // topic == <username>/admin
			Serial.println("Admin message...");
			
			if (length == 9 && strncmp("askstatus", data, 9) == 0) {
				Serial.println("Retrieving status");
				
				report_status = (digitalRead(relaypin)) ? "on" : "off";
			}
			else if (length > 4 && strncmp("time", data, 4) == 0) {  // time synchronization
				// low-precision, just to keep the device time from drifting away through the year
				// the server should send this as soon as the device is connected and once a day
				// format: time EpochTime
				// EpochTime is the number of seconds since 1 Jan 1970, 00:00:00
				// e.g. time 1437495683 means 'set your clock to July 21 2015, 13:21:23'
				
				char buffer[16];  // hopefully we have moved on from relying on C overflowable arrays
				                  // by the time this buffer can't hold the corresponding EpochTime
				
				unsigned int i = 4;
				
				Serial.println("Synchronizing");
				
				if (i >= length || !std::isspace(data[i])) {
					Serial.printf("'Time' packet: Incorrect format at %d: expected whitespace, found %c\r\n", i, data[i]);
					return;
				}
				
				while (i < length && std::isspace(data[i])) {
					i += 1;
				}
				
				int timelen = length - i;
				
				if (timelen > 15) {
					Serial.println("'Time' packet: time string too long");
					return;
				}
				
				strncpy(buffer, &data[i], timelen);
				buffer[timelen] = 0;
				
				const char* readend;
				uint64_t    time = readull(buffer, &readend);
				
				if (*readend != 0) {  // readend will point to the string null terminator if the string was parsed completely
					Serial.println("'Time' packet: can't read timestamp");
					return;
				}
				
				curdate    = time;
				lastmillis = millis();
				
				calculatenextfire();
				updatescallback();
			}
		}
	}
	
	if (clen == 5 && strncmp("lobby", channel, 5) == 0) {
		if (length == 4 && strncmp("ping", data, 4) == 0) {
			Serial.println("Pinging back");
			should_ping = true;
		}
		else if (length > 5 && strncmp("auth\n", data, 5) == 0) {
			// the payload consists of three lines: 'auth', user and password
			unsigned int i = 5;
			
			Serial.println("Parsing credentials");
			
			for (; i < length; i++) {
				if (payload[i] == '\n') {
					int userlen = i - 5;
					int passlen = length - i - 1;
					if (userlen > maxcfgstrsize - 1) {
						Serial.println("Received MQTT username is too long");
						return;
					}
					
					if (passlen > maxcfgstrsize - 1) {
						Serial.println("Received MQTT password is too long");
						return;
					}
					
					strncpy(settings.mqtt_user, &data[5], userlen);
					settings.mqtt_user[userlen] = 0;
					
					strncpy(settings.mqtt_pass, &data[i + 1], passlen);
					settings.mqtt_pass[passlen] = 0;
					
					flush_settings();
					
					mqtt_hascreds    = true;
					should_reconnect = true;
					mqtt.disconnect();
				}
			}
		}
	}
}

// Main functions
// ------------------------------------------------------------------------------

bool          buttonprev;          // button state in the previous loop iteration
unsigned long buttonstart;         // timestamp for the start of a button press
unsigned long mqtt_lastreconnect;  // timestamp for the last time a MQTT reconnection was attempted
unsigned long mqtt_lastaskpass;    // timestamp for the last time the client asked for credentials
int           mqtt_attempts;       // number of consecutive reconnection attempts so far
bool          should_askpass;      // true if enough time has pass to ask for credentials again

// set up all resources
void setup()
{
	// general settings
	
	Serial.begin(115200);
	pinMode(ledpin, OUTPUT);
	
	delay(2000);
	
	Serial.println();
	Serial.println("Starting Sonoff wireless switch");
	ledblink(1);
	
	pinMode(buttonpin, INPUT);
	buttonprev = (digitalRead(buttonpin) == BTN_PRESSED);
	
	digitalWrite(relaypin, digitalRead(relaypin));  // <-- this  may seem to do nothing, but it changes the internal buffer
	pinMode(relaypin, OUTPUT);                      // so this OUTPUT mode change does not force an unnecessary relay switch
	                                                // (i.e. reset should not turn the lights off!)
	
	// read wifi settings and set it up
	
	EEPROM.begin(sizeof (Settings));
	EEPROM.get(0, settings);
	
	uint32_t checksum = settings_checksum(&settings);
	
	if (settings.checksum != checksum) {
		Serial.println("Incorrect settings checksum, using default values");
		Serial.printf("checksum was %d (%x) but %d (%x) was expected\r\n", settings.checksum, settings.checksum, checksum, checksum);
		Serial.printf("username was %s\r\n", settings.mqtt_user);
		
#if DEBUG_SETTINGS
		Serial.println("old settings");
		dump_settings();
#endif
		
		settings = Settings();
		settings.checksum = settings_checksum(&settings);
		EEPROM.put(0, settings);

#if DEBUG_SETTINGS
		Serial.println("new settings");
		dump_settings();
#endif
	}
	
	EEPROM.end();
	
	randomSeed(RANDOM_REG32 ^ micros());  // RANDOM_REG32 uses an internal (undocumented) hardware-based PRNG
	delay(random(0, 2000));  // random delay to reduce congestion if multiple devices are turned on at the same time
	
	Serial.print("Connecting to WiFi");
	
	WiFi.mode(WIFI_STA);
	WiFi.begin(settings.ssid, settings.password);
	
	for (int i = 0; WiFi.status() != WL_CONNECTED && i < 60; i++) {  // max wait time: 24 secs
		delay(400);
		Serial.print(".");
	}
	
	Serial.println("");
	
	if (WiFi.status() != WL_CONNECTED) {
		Serial.println("WiFi connection failed");
		restart();
	}
	
	randomSeed(RANDOM_REG32 ^ micros());  // repeat the seed after connecting to WiFi for a better source of entropy:
	delay(random(0, 2000));               // the microseconds the connection took (RANDOM_REG_32 seems to be legitimately
	                                      // random at all times, but it is undocumented, so let's not assume anything)
	
	// connect with server
	
	Serial.println("Getting Server IP");
	
	if (!MDNS.begin("")) {
		Serial.println("Cannot start mDNS");
		restart();
	}
	
	masterip      = MDNS.queryHost(masterhost);
	ip_addr_t mip = {masterip};
	
	dns_local_addhost(masterhost, &mip);
	
	if (masterip == IPAddress()) {
		Serial.println("Server host not found in mDNS");
		restart();
	}
	
	Serial.print("Server found at ");
	Serial.println(masterip.toString());
	
	// firmware OTA update
	
	Serial.println("Checking for firmware upgrade candidates");
	
	int retvalue = ESPhttpUpdate.update(masterhost, masterporthttps, firmwareuri, String(version), fingerprint);
	if (retvalue == HTTP_CODE_OK) {
		Serial.println("Found OTA firmware. Upgrading...");
		restart();
	}
	if (retvalue < 0) {
		Serial.println("Can't read OTA firmware, continuing");
	}
	else if (retvalue == HTTP_CODE_NOT_MODIFIED) {
		Serial.println("Found OTA firmware. Not a new version, skipping");
	}
	else {
		Serial.println("No upgrades found");
	}
	
	// WiFi reconfiguration
	
	HTTPClient client;
	
	Serial.println("Checking for access credentials updates");
	
	if (client.begin(masterhost, masterporthttps, accessuri, fingerprint) && client.GET() == HTTP_CODE_NO_CONTENT) {
		String candidatessid = client.header("X-SSID");
		String candidatepass = client.header("X-PSK");
		
		if (candidatessid != settings.ssid || candidatepass != settings.password) {
			if (candidatessid.length() > maxcfgstrsize - 1) {
				Serial.println("Read WiFi SSID too long");
				restart();
			}
			
			if (candidatepass.length() > maxcfgstrsize - 1) {
				Serial.println("Read WiFi password too long");
				restart();
			}
			
			candidatessid.toCharArray(settings.ssid, maxcfgstrsize);
			candidatepass.toCharArray(settings.password, maxcfgstrsize);
			
			flush_settings();
			
			Serial.println("Access credentials updated");
		}
		else {
			Serial.println("Found access credentials. Not modified, continuing");
		}
	}
	else {
		Serial.println("Can't read access updates, continuing");
	}
	
	// MQTT configuration
	
	Serial.println("Configuring MQTT client");
	
	mqtt_hascreds = (strlen(settings.mqtt_user) > 0 && strlen(settings.mqtt_pass) > 0);
	
	if (!mqtt_hascreds) {
		(mqtt_prefix + String(static_cast<unsigned long>(random(INT_MIN, INT_MAX)), HEX)).toCharArray(settings.mqtt_user, maxcfgstrsize);
	}
	
	Serial.print("MQTT username: ");
	Serial.println(settings.mqtt_user);
	
	int now = millis();
	
	mqtt_attempts      = 0;
	mqtt_lastreconnect = now;
	mqtt_lastaskpass   = now;
	should_askpass     = true;
	should_reconnect   = true;
	should_ping        = false;
	report_status      = nullptr;
	
	mqtt.setServer(masterhost, masterportmqtt);
	mqtt.setCallback(mqtt_receive);
	
	Serial.println("Sonoff setup completed");
	Serial.println("------------------------");
	Serial.println();
	Serial.println();
	ledblink(2);
	
	buttonstart = millis();  // if the user hasn't lifted the button since the program started,
	                         // start counting the button press from here, the end of the setup
}

// main loop
void loop()
{
	// read user input (button)
	bool          buttonstate = (digitalRead(buttonpin) == BTN_PRESSED);
	unsigned long now         = millis();
	
	if (!buttonprev && buttonstate) {  // OnButtonDown
		buttonstart = now;
	}
	else if (buttonstate) { // && buttonprev (implicit); OnButtonPressed
		if (now - buttonstart > 5000) {  // 5 seconds: restart
			Serial.println("Button pressed for 5 seconds, restarting");
			restart();
		}
	}
	else if (buttonprev) {  // && !buttonstate (implicit): OnButtonUp
		if (now - buttonstart > 200) {  // 0.2 seconds: toggle relay
			Serial.print("relay -> toggle (");
			Serial.print(!digitalRead(relaypin) ? "on" : "off");
			Serial.println(")");
			report_status = (!digitalRead(relaypin)) ? "on" : "off";
			digitalWrite(relaypin, !digitalRead(relaypin));
		}
	}
	
	// MQTT
	if (mqtt.connected()) {
		mqtt.loop();
		
		if (should_ping) {
			should_ping = false;
			mqtt.publish(lobbytopic, "here");
		}
		
		if (report_status != nullptr) {
			
			char message[12];  // max len = 10 -> "status off"
			int  slen = strlen(report_status);
			
			strncpy(message, "status ", 7);
			strncpy(&message[7], report_status, 12 - 7);
			message[11] = 0;
			
			mqtt.publish(admintopic, message);
			
			report_status = nullptr;
		}
		
		if (!mqtt_hascreds && should_askpass) {
			should_askpass = false;
			Serial.println("Asking for credentials");
			mqtt_lastaskpass = now;
			askcredentials();
		}
		
		if (!mqtt_hascreds && now - mqtt_lastaskpass > 2 * 60 * 1000) {  // ask for a password every 2 minutes
			should_askpass = true;
		}
	}
	else {
		if (should_reconnect) {
			mqtt_lastreconnect = now;
			should_reconnect   = false;
			
			if (mqtt_connect()) {
				Serial.println("Successfully connected to MQTT broker");
				should_askpass = true;
				mqtt_attempts  = 0;
			}
			else {
				Serial.println("Failed to connect to MQTT broker");
				mqtt_attempts += 1;
				
				if (mqtt_attempts > mqtt_maxattempts) {  // something has gone wrong!
					Serial.println("Too many failed MQTT connections, restarting");
					restart();                           // start again from a clean state
				}
			}
		}
		
		if (now - mqtt_lastreconnect > 5000) {  // reconnect to MQTT every 5 seconds
			should_reconnect = true;
		}
	}
	
	delay(250);
}
