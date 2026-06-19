import time
import sys
import machine
import dht
import network
import urequests
import ujson
import uhashlib
import ubinascii
from struct import pack

DEBUG = False

# Load config
with open('config.json') as f:
    config = ujson.load(f)

WIFI_SSID = config['WIFI_SSID']
WIFI_PASS = config['WIFI_PASS']
AWS_ACCESS_KEY = config['AWS_ACCESS_KEY']
AWS_SECRET_KEY = config['AWS_SECRET_KEY']
AWS_REGION = config['AWS_REGION']

RELAY_PIN = 0
DHT_PIN = 22
HUMIDITY_THRESHOLD = 80
HUMIDIFIER_DURATION = 1 * 60  # seconds

relay = machine.Pin(RELAY_PIN, machine.Pin.OUT)
relay.value(0)

led = machine.Pin('LED', machine.Pin.OUT)

sensor = dht.DHT22(machine.Pin(DHT_PIN))


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print('Connecting to WiFi...')
        wlan.connect(WIFI_SSID, WIFI_PASS)
        for _ in range(20):
            if wlan.isconnected():
                break
            time.sleep(1)
    if wlan.isconnected():
        print(f'Connected: {wlan.ifconfig()[0]}')
    else:
        print('WiFi connection failed')
    return wlan


def get_timestamp():
    t = time.gmtime()
    return '{:04d}{:02d}{:02d}T{:02d}{:02d}{:02d}Z'.format(*t[:6])


def get_datestamp():
    t = time.gmtime()
    return '{:04d}{:02d}{:02d}'.format(*t[:3])


def hmac_sha256(key, msg):
    if isinstance(key, str):
        key = key.encode()
    if isinstance(msg, str):
        msg = msg.encode()
    block_size = 64
    if len(key) > block_size:
        h = uhashlib.sha256(key)
        key = h.digest()
    key = key + b'\x00' * (block_size - len(key))
    o_key_pad = bytes([k ^ 0x5c for k in key])
    i_key_pad = bytes([k ^ 0x36 for k in key])
    h_inner = uhashlib.sha256(i_key_pad + msg)
    inner = h_inner.digest()
    h_outer = uhashlib.sha256(o_key_pad + inner)
    return h_outer.digest()


def sha256(data):
    if isinstance(data, str):
        data = data.encode()
    h = uhashlib.sha256(data)
    return ubinascii.hexlify(h.digest()).decode()


def sign(key, msg):
    return hmac_sha256(key, msg)


def get_signature_key(key, datestamp, region, service):
    k_date = sign(('AWS4' + key).encode(), datestamp)
    k_region = sign(k_date, region)
    k_service = sign(k_region, service)
    k_signing = sign(k_service, 'aws4_request')
    return k_signing


def aws_request(service, target, body):
    host = f'{service}.{AWS_REGION}.amazonaws.com'
    endpoint = f'https://{host}/'
    t = get_timestamp()
    d = get_datestamp()
    content_type = 'application/x-amz-json-1.1' if service == 'logs' else 'application/x-amz-json-1.0'

    headers_to_sign = f'content-type;host;x-amz-date;x-amz-target'
    canonical_headers = (
        f'content-type:{content_type}\n'
        f'host:{host}\n'
        f'x-amz-date:{t}\n'
        f'x-amz-target:{target}\n'
    )
    payload_hash = sha256(body)
    canonical_request = f'POST\n/\n\n{canonical_headers}\n{headers_to_sign}\n{payload_hash}'
    credential_scope = f'{d}/{AWS_REGION}/{service}/aws4_request'
    string_to_sign = f'AWS4-HMAC-SHA256\n{t}\n{credential_scope}\n{sha256(canonical_request)}'
    signing_key = get_signature_key(AWS_SECRET_KEY, d, AWS_REGION, service)
    signature = ubinascii.hexlify(sign(signing_key, string_to_sign)).decode()
    auth_header = (
        f'AWS4-HMAC-SHA256 Credential={AWS_ACCESS_KEY}/{credential_scope}, '
        f'SignedHeaders={headers_to_sign}, Signature={signature}'
    )

    headers = {
        'Content-Type': content_type,
        'X-Amz-Date': t,
        'X-Amz-Target': target,
        'Authorization': auth_header,
    }
    try:
        if isinstance(body, str):
            body = body.encode()
        r = urequests.post(endpoint, data=body, headers=headers)
        if r.status_code != 200:
            print(f'AWS {service} error {r.status_code}: {r.text}')
        r.close()
    except Exception as e:
        print(f'AWS request error: {e}')


LOG_GROUP = 'RaspberryPiHumidifier'
LOG_STREAM = 'humidifier-py'


def log(msg):
    print(msg)
    try:
        body = ujson.dumps({
            'logGroupName': LOG_GROUP,
            'logStreamName': LOG_STREAM,
            'logEvents': [{'timestamp': int(time.time() * 1000), 'message': msg}]
        })
        aws_request('logs', 'Logs_20140328.PutLogEvents', body)
    except Exception as e:
        print(f'Log error: {e}')


def read_dht():
    for _ in range(5):
        try:
            sensor.measure()
            return sensor.temperature(), sensor.humidity()
        except OSError:
            time.sleep(2)
    return None, None


def get_onboard_temp():
    adc = machine.ADC(4)
    raw = adc.read_u16()
    voltage = raw * 3.3 / 65535
    return 27 - (voltage - 0.706) / 0.001721


def send_metrics():
    humidity = None
    try:
        temp, humidity = read_dht()
        onboard_temp = get_onboard_temp()
        temp_f = temp * 9/5 + 32 if temp is not None else None
        onboard_f = onboard_temp * 9/5 + 32 if onboard_temp is not None else None

        metrics = []
        if temp_f is not None:
            metrics.append({'MetricName': 'Temperature', 'Value': temp_f, 'Unit': 'None'})
        if humidity is not None:
            metrics.append({'MetricName': 'Humidity', 'Value': humidity, 'Unit': 'Percent'})
        if onboard_f is not None:
            metrics.append({'MetricName': 'CPUTemperature', 'Value': onboard_f, 'Unit': 'None'})

        if metrics:
            body = ujson.dumps({'Namespace': 'RaspberryPiHumidifier', 'MetricData': metrics})
            aws_request('monitoring', 'GraniteServiceVersion20100801.PutMetricData', body)

        t = time.localtime()
        ts = '{:02d}:{:02d}:{:02d}'.format(t[3], t[4], t[5])
        log(f'[{ts}] Metrics sent — temp={temp_f:.1f}°F, humidity={humidity}%, cpu={onboard_f:.1f}°F')
    except Exception as e:
        log(f'Metric error: {e}')
    return humidity


def sync_time():
    try:
        import ntptime
        ntptime.settime()
        print('Time synced via NTP')
    except Exception as e:
        print(f'NTP sync failed: {e}')


try:
    wlan = connect_wifi()
    sync_time()
    log('Humidifier Pico is on!')
    last_metric_time = 0

    while True:
        try:
            led.value(1)
            now = time.time()
            interval = 10 if DEBUG else 600

            if now - last_metric_time >= interval:
                humidity = send_metrics()
                last_metric_time = now

                if humidity is not None and humidity < HUMIDITY_THRESHOLD:
                    duration = 10 if DEBUG else HUMIDIFIER_DURATION
                    t = time.localtime()
                    ts = '{:02d}:{:02d}:{:02d}'.format(t[3], t[4], t[5])
                    log(f'[{ts}] Humidity {humidity:.1f}% below {HUMIDITY_THRESHOLD}% — running for {duration}s')
                    relay.value(1)
                    time.sleep(duration)
                    relay.value(0)
                    t = time.localtime()
                    ts = '{:02d}:{:02d}:{:02d}'.format(t[3], t[4], t[5])
                    log(f'[{ts}] Humidifier OFF')
                else:
                    t = time.localtime()
                    ts = '{:02d}:{:02d}:{:02d}'.format(t[3], t[4], t[5])
                    log(f'[{ts}] Humidity {humidity:.1f}% OK, not needed')

            led.value(0)
            time.sleep(1 if DEBUG else 60)
        except Exception as e:
            log(f'Loop error: {e}')
            led.value(0)
            time.sleep(1 if DEBUG else 60)
except KeyboardInterrupt:
    relay.value(0)
