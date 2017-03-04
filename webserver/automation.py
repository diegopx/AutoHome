#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# automation.py
# an automation development server
# by Diego Guerrero

import flask
import ssl
import sys
import json

app = flask.Flask(__name__)
credentials = None

def check_authorization():
	return flask.request.headers.get("Authorization") == credentials["psk"]

@app.route("/static/sonoff-firmware.bin", methods=["GET"])
def sonoff_firmware():
	
	if not check_authorization():
		flask.abort(404)
	
	try:
		version = int(flask.request.headers.get("X-ESP8266-version"))
	except:
		version = sys.maxint
	
	available = 1
	
	if version < available:
		return flask.send_from_directory("static/", "sonoff-firmware.bin")
	else:
		return ("", 304, {})
	
@app.route("/static/access", methods=["GET"])
def access():
	if not check_authorization():
		flask.abort(404)
	
	return ("", 204, {"X-SSID": credentials["wifi-ssid"], "X-PSK": credentials["wifi-pass"]})

def main():
	try:
		with open("credentials.json") as credfile:
			credentials = json.loads(credfile.read())
	except IOError:
		print("Can't open credential file", file=sys.stderr)
		return
	
	sslcontext = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
	sslcontext.load_cert_chain(credentials["certificate"], credentials["keyfile"])
	
	app.run(host="automation.local", port=8266, ssl_context=sslcontext, threaded=False, debug=False)

if __name__ == "__main__":
	main()
