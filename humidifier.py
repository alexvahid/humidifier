import time
import sys
import board
import adafruit_dht
import RPi.GPIO as GPIO
import boto3
from datetime import datetime

DEBUG = '--debug' in sys.argv

RELAY_PIN = 17
DHT_PIN = board.D4
HUMIDITY_THRESHOLD = 80  # Turn on humidifier if humidity drops below this %
HUMIDIFIER_DURATION = 1 * 60  # seconds

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(RELAY_PIN, GPIO.OUT)
GPIO.output(RELAY_PIN, GPIO.LOW)

cloudwatch = boto3.client('cloudwatch', region_name='us-east-1')
logs = boto3.client('logs', region_name='us-east-1')
LOG_GROUP = 'RaspberryPiHumidifier'
LOG_STREAM = 'humidifier-py'

dht = adafruit_dht.DHT22(DHT_PIN)


def log(msg):
    print(msg)
    try:
        logs.put_log_events(
            logGroupName=LOG_GROUP,
            logStreamName=LOG_STREAM,
            logEvents=[{'timestamp': int(time.time() * 1000), 'message': msg}]
        )
    except Exception as e:
        print(f"Log error: {e}")


def read_dht():
    for _ in range(5):
        try:
            return dht.temperature, dht.humidity
        except RuntimeError:
            time.sleep(2)
    return None, None


def get_cpu_temp():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return float(f.read()) / 1000.0
    except:
        return None


def send_metrics():
    temp, humidity, cpu_temp = None, None, None
    try:
        temp, humidity = read_dht()
        cpu_temp = get_cpu_temp()
        metrics = []
        temp_f = temp * 9/5 + 32 if temp is not None else None
        cpu_temp_f = cpu_temp * 9/5 + 32 if cpu_temp is not None else None
        if temp_f is not None:
            metrics.append({'MetricName': 'Temperature', 'Value': temp_f, 'Unit': 'None'})
        if humidity is not None:
            metrics.append({'MetricName': 'Humidity', 'Value': humidity, 'Unit': 'Percent'})
        if cpu_temp_f is not None:
            metrics.append({'MetricName': 'CPUTemperature', 'Value': cpu_temp_f, 'Unit': 'None'})
        if metrics:
            cloudwatch.put_metric_data(Namespace='RaspberryPiHumidifier', MetricData=metrics)
        log(f"[{datetime.now():%H:%M:%S}] Metrics sent — temp={temp_f:.1f}°F, humidity={humidity}%, cpu={cpu_temp_f:.1f}°F")
    except Exception as e:
        log(f"Metric error: {e}")
    return humidity


try:
    log("Humidifier Pi is on!")
    last_metric_time = datetime.min

    while True:
        try:
            now = datetime.now()

            if (now - last_metric_time).total_seconds() >= (10 if DEBUG else 600):
                humidity = send_metrics()
                last_metric_time = now

                if humidity is not None and humidity < HUMIDITY_THRESHOLD:
                    duration = 10 if DEBUG else HUMIDIFIER_DURATION
                    log(f"[{now:%H:%M:%S}] Humidity {humidity:.1f}% below threshold {HUMIDITY_THRESHOLD}% — running humidifier for {duration}s")
                    GPIO.output(RELAY_PIN, GPIO.HIGH)
                    time.sleep(duration)
                    GPIO.output(RELAY_PIN, GPIO.LOW)
                    log(f"[{datetime.now():%H:%M:%S}] Humidifier OFF")
                else:
                    log(f"[{now:%H:%M:%S}] Humidity {humidity:.1f}% OK, humidifier not needed")

            time.sleep(1 if DEBUG else 60)
        except Exception as e:
            log(f"Loop error: {e}")
            time.sleep(1 if DEBUG else 60)
except KeyboardInterrupt:
    dht.exit()
    GPIO.cleanup()
