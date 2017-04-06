#!/bin/bash

killchildren()
{
	pid="$1"
	for child in $(ps -o pid,ppid -ax | awk "{ if ( \$2 == $pid ) { print \$1 }}")
	do
		kill $child
	done
}

ip=$(ip -4 addr show wlan0 | grep -oP "(?<=inet\s)\d+(\.\d+){3}")

trap 'if [ -n "$appid" ]; then killchildren $appid &> /dev/null; fi; if [ -n "$wspid" ]; then killchildren $wspid &> /dev/null; fi; wait' INT TERM HUP EXIT

echo "Publishing domain name"
exec 3< <(avahi-publish -a -R autohome.local $ip 2>&1)
appid=$!
sed "/Established under name/q" <&3 ; cat <&3 &

echo "Starting AutoHome HTTP resource server"
exec 4< <(python3 devcontrol/http/devcontent.py 2>&1)
wspid=$!
sed "/Running on/q" <&4 ; cat <&4 &

python3 devcontrol/devcontrol.py
