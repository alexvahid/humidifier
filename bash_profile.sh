source /home/pi/humidifier/humidifierenv/bin/activate
cd /home/pi/humidifier
git pull --timeout=5 2>/dev/null || true
cp ./bash_profile.sh ~/.bash_profile
python3 /home/pi/humidifier/humidifier.py &
