#!/bin/bash

ip=$(ip -4 addr show wlan0 | grep -oP "(?<=inet\s)\d+(\.\d+){3}")

trap 'if [ -z "$appid" ]; then kill $appid $wspid &> /dev/null; fi' INT TERM HUP EXIT

echo "Publishing domain name"
exec 3< <(avahi-publish -a -R automation.local $ip 2>&1)
appid=$!
sed "/Established under name/q" <&3 ; cat <&3 &

echo "Starting AutoHome HTTP web server"
exec 4< <(python3 webserver/automation.py 2>&1)
wspid=$!
sed "/Running on/q" <&4 ; cat <&4 &

python3 autohome.py
