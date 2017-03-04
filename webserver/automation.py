#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# automation.py
# an automation development server
# by Diego Guerrero

import flask
import ssl

app = flask.Flask(__name__)

def check_authorization():
	return flask.request.headers.get("Authorization") == "tPO8EmdwbGnFnADAPcqWbY8aYAsOre"

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
	
	return ("", 204, {"X-SSID": "Kaos86-ext", "X-PSK": "fKeZx/@bmW[h_rI.BlF42WX+Hjl"})

if __name__ == "__main__":
	sslcontext = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
	
	sslcontext.load_cert_chain("keys/local.crt", "keys/local.key")
	
	app.run(host="automation.local", port=8266, ssl_context=sslcontext, threaded=False, debug=False)
