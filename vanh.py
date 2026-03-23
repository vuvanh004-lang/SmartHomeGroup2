from flask import Flask, render_template, request, jsonify
import RPi.GPIO as GPIO
import time
import threading
import adafruit_dht
import board
import smbus
import requests
import sqlite3
from datetime import datetime
from mfrc522 import SimpleMFRC522

app = Flask(__name__)

LDR_PIN = 17
PIR_PIN = 4
BUZZER_PIN = 22
RGB_R, RGB_G, RGB_B = 16, 21, 23
FAN_RELAY = 27
LED_RELAY = 20
SERVO_PIN = 18

SW_LED = 26
SW_DOOR = 12

GAS_CLK = 5
GAS_DOUT = 6
GAS_DIN = 13
GAS_CS = 19

I2C_ADDR = 0x27
LCD_WIDTH = 16
LCD_CHR = 1
LCD_CMD = 0
LCD_LINE_1 = 0x80
LCD_LINE_2 = 0xC0
LCD_BACKLIGHT = 0x08
ENABLE = 0b00000100

AUTHORIZED_UID = [181213127134, 182667064662, 18051368384]
TG_BOT_TOKEN = "8790257042:AAEz4Ub3Oo4EAx4yWJgFbFnlQmOfiAk5LrE"
TG_CHAT_ID = "1478699789"

try:
    bus = smbus.SMBus(1)
except Exception:
    pass

def lcd_toggle_enable(bits):
    time.sleep(0.001)
    bus.write_byte(I2C_ADDR, (bits | ENABLE))
    time.sleep(0.001)
    bus.write_byte(I2C_ADDR, (bits & ~ENABLE))
    time.sleep(0.001)

def lcd_byte(bits, mode):
    bits_high = mode | (bits & 0xF0) | LCD_BACKLIGHT
    bits_low = mode | ((bits << 4) & 0xF0) | LCD_BACKLIGHT
    try:
        bus.write_byte(I2C_ADDR, bits_high)
        lcd_toggle_enable(bits_high)
        bus.write_byte(I2C_ADDR, bits_low)
        lcd_toggle_enable(bits_low)
    except Exception:
        pass

def lcd_init():
    for b in [0x33, 0x32, 0x06, 0x0C, 0x28, 0x01]:
        lcd_byte(b, LCD_CMD)
    time.sleep(0.005)

def lcd_string(message, line):
    message = message[:LCD_WIDTH].ljust(LCD_WIDTH, " ")
    lcd_byte(line, LCD_CMD)
    for i in range(LCD_WIDTH):
        lcd_byte(ord(message[i]), LCD_CHR)

def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=3)
    except Exception:
        pass

def init_db():
    conn = sqlite3.connect('sensor_log.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS dht_data 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  time TEXT, temperature REAL, humidity REAL)''')
    conn.commit()
    conn.close()

def log_to_db(temp, hum):
    try:
        conn = sqlite3.connect('sensor_log.db')
        c = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO dht_data (time, temperature, humidity) VALUES (?, ?, ?)", (current_time, temp, hum))
        conn.commit()
        conn.close()
    except Exception:
        pass

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

out_pins = [BUZZER_PIN, RGB_R, RGB_G, RGB_B, FAN_RELAY, LED_RELAY, SERVO_PIN, GAS_CLK, GAS_DIN, GAS_CS]
for pin in out_pins:
    GPIO.setup(pin, GPIO.OUT)

in_pins = [GAS_DOUT, PIR_PIN, LDR_PIN, SW_LED, SW_DOOR]
for pin in in_pins:
    GPIO.setup(pin, GPIO.IN)

RELAY_ON = GPIO.LOW
RELAY_OFF = GPIO.HIGH

GPIO.output(FAN_RELAY, RELAY_OFF)
GPIO.output(LED_RELAY, RELAY_OFF)
GPIO.output(BUZZER_PIN, GPIO.LOW)
GPIO.output(GAS_CS, True)

servo_pwm = GPIO.PWM(SERVO_PIN, 50)
servo_pwm.start(2.5)

dht_device = adafruit_dht.DHT22(board.D14, use_pulseio=False)
rfid_reader = SimpleMFRC522()

sys_data = {
    "temp": 0.0, "hum": 0.0, "gas": 0, 
    "mode": "AUTO", "door": "CLOSED", "gas_alert": False
}
manual_state = {"LED": False, "RGB": False, "FAN": False, "DOOR": False}

try:
    lcd_init()
    init_db()
except Exception:
    pass

def read_gas():
    GPIO.output(GAS_CS, True)
    GPIO.output(GAS_CLK, False)
    GPIO.output(GAS_CS, False)
    cmd = 0x18 << 3
    for i in range(5):
        GPIO.output(GAS_DIN, bool(cmd & 0x80))
        cmd <<= 1
        GPIO.output(GAS_CLK, True)
        GPIO.output(GAS_CLK, False)
    adcout = 0
    for i in range(12):
        GPIO.output(GAS_CLK, True)
        GPIO.output(GAS_CLK, False)
        adcout = (adcout << 1) | GPIO.input(GAS_DOUT)
    GPIO.output(GAS_CS, True)
    return adcout >> 1

def set_rgb(r, g, b):
    GPIO.output(RGB_R, r)
    GPIO.output(RGB_G, g)
    GPIO.output(RGB_B, b)

def main_loop():
    global sys_data, manual_state
    
    last_dht_read = 0
    last_tg_gas = 0
    last_tg_intruder = 0
    last_db_log_time = 0
    
    rfid_display_timer = 0
    rfid_active_until = 0
    rfid_msg_1 = ""
    rfid_msg_2 = ""
    
    last_sw_led = GPIO.input(SW_LED)
    last_sw_door = GPIO.input(SW_DOOR)
    
    current_lcd_l1 = ""
    current_lcd_l2 = ""

    while True:
        try:
            # Lưu DataBase moi 60 giay
            if time.time() - last_db_log_time >= 60:
                if sys_data["temp"] > 0:
                    log_to_db(sys_data["temp"], sys_data["hum"])
                last_db_log_time = time.time()

            sw_led_curr = GPIO.input(SW_LED)
            sw_door_curr = GPIO.input(SW_DOOR)
            
            if sw_led_curr != last_sw_led:
                sys_data["mode"] = "MANUAL"
                manual_state["LED"] = bool(sw_led_curr)
                last_sw_led = sw_led_curr
                
            if sw_door_curr != last_sw_door:
                sys_data["mode"] = "MANUAL"
                manual_state["DOOR"] = bool(sw_door_curr)
                last_sw_door = sw_door_curr

            gas_val = read_gas()
            sys_data["gas"] = gas_val
            is_gas_leak = gas_val > 400
            sys_data["gas_alert"] = is_gas_leak
            
            pir_val = GPIO.input(PIR_PIN)
            ldr_val = GPIO.input(LDR_PIN)

            # Xu ly Quet The RFID
            uid, text = rfid_reader.read_no_block()
            if uid:
                rfid_display_timer = time.time()
                if uid in AUTHORIZED_UID:
                    rfid_msg_1 = "  WELCOME HOME  "
                    rfid_msg_2 = "   DOOR OPEN    "
                    rfid_active_until = time.time() + 3  # Cho cua va den mo trong 3 giay
                else:
                    rfid_msg_1 = "   WRONG CARD   "
                    rfid_msg_2 = " ACCESS DENIED  "
                    GPIO.output(BUZZER_PIN, GPIO.HIGH)
                    set_rgb(1, 0, 1)
                    time.sleep(1)
                    GPIO.output(BUZZER_PIN, GPIO.LOW)
                    set_rgb(0, 0, 0)

            # Chuan bi chuoi ky tu cho LCD
            target_l1 = f"T:{sys_data['temp']}C H:{sys_data['hum']}%"
            target_l2 = f"Gas: {sys_data['gas']}"

            # Logic Uu Tien 1: Bao dong Gas
            if is_gas_leak:
                target_l1 = "!! GAS LEAK !!"
                target_l2 = "EVACUATE NOW"
                
                GPIO.output(BUZZER_PIN, GPIO.HIGH)
                GPIO.output(FAN_RELAY, RELAY_ON)
                servo_pwm.ChangeDutyCycle(7.5)
                sys_data["door"] = "OPEN"
                
                if int(time.time() * 4) % 2 == 0:
                    set_rgb(1, 0, 0)
                else:
                    set_rgb(0, 0, 0)
                    
                if time.time() - last_tg_gas > 60:
                    threading.Thread(target=send_telegram_msg, args=("[EMERGENCY] GAS LEAK DETECTED!",), daemon=True).start()
                    last_tg_gas = time.time()

            else:
                last_tg_gas = 0
                current_door_pwm = 2.5
                sys_data["door"] = "CLOSED"
                
                # Logic Uu Tien 2: Quet the thanh cong (Mo cua & den tam thoi 3 giay)
                is_rfid_active = (time.time() < rfid_active_until)
                
                if sys_data["mode"] == "AUTO":
                    GPIO.output(BUZZER_PIN, GPIO.LOW)
                    
                    if is_rfid_active or ldr_val == 1:
                        GPIO.output(LED_RELAY, RELAY_ON)
                        if ldr_val == 1: set_rgb(1, 1, 1)
                    else:
                        GPIO.output(LED_RELAY, RELAY_OFF)
                        set_rgb(0, 0, 0)
                        
                    if sys_data["temp"] > 29.0:
                        GPIO.output(FAN_RELAY, RELAY_ON)
                    else:
                        GPIO.output(FAN_RELAY, RELAY_OFF)
                        
                    if is_rfid_active:
                        current_door_pwm = 7.5
                        sys_data["door"] = "OPEN"
                        
                    servo_pwm.ChangeDutyCycle(current_door_pwm)

                elif sys_data["mode"] == "MANUAL":
                    GPIO.output(BUZZER_PIN, GPIO.LOW)
                    
                    GPIO.output(LED_RELAY, RELAY_ON if (manual_state["LED"] or is_rfid_active) else RELAY_OFF)
                    set_rgb(1, 1, 1) if manual_state["RGB"] else set_rgb(0, 0, 0)
                    GPIO.output(FAN_RELAY, RELAY_ON if manual_state["FAN"] else RELAY_OFF)
                    
                    if manual_state["DOOR"] or is_rfid_active:
                        current_door_pwm = 7.5
                        sys_data["door"] = "OPEN"
                    
                    servo_pwm.ChangeDutyCycle(current_door_pwm)

                elif sys_data["mode"] == "NIGHT":
                    GPIO.output(FAN_RELAY, RELAY_OFF)
                    
                    if is_rfid_active:
                        current_door_pwm = 7.5
                        sys_data["door"] = "OPEN"
                        GPIO.output(LED_RELAY, RELAY_ON)
                    else:
                        current_door_pwm = 2.5
                        sys_data["door"] = "CLOSED"
                    
                    servo_pwm.ChangeDutyCycle(current_door_pwm)
                    
                    if pir_val == 1:
                        target_l1 = "!! INTRUDER !!"
                        target_l2 = "MOTION DETECTED"
                        GPIO.output(BUZZER_PIN, GPIO.HIGH)
                        GPIO.output(LED_RELAY, RELAY_ON)
                        set_rgb(1, 1, 1)
                        if time.time() - last_tg_intruder > 60:
                            threading.Thread(target=send_telegram_msg, args=("[SECURITY] Motion detected in Night Mode!",), daemon=True).start()
                            last_tg_intruder = time.time()
                    else:
                        GPIO.output(BUZZER_PIN, GPIO.LOW)
                        if not is_rfid_active:
                            GPIO.output(LED_RELAY, RELAY_OFF)
                        set_rgb(0, 0, 0)

            # Ghi de hien thi LCD trong 3 giay neu co the quet
            if time.time() - rfid_display_timer < 3:
                target_l1 = rfid_msg_1
                target_l2 = rfid_msg_2

            # Chi cap nhat LCD khi noi dung thay doi (Chong giat LCD)
            if target_l1 != current_lcd_l1 or target_l2 != current_lcd_l2:
                try:
                    lcd_string(target_l1, LCD_LINE_1)
                    lcd_string(target_l2, LCD_LINE_2)
                    current_lcd_l1 = target_l1
                    current_lcd_l2 = target_l2
                except Exception:
                    pass

            # Cap nhat cam bien DHT22 moi 2 giay
            if time.time() - last_dht_read > 2.0:
                try:
                    t = dht_device.temperature
                    h = dht_device.humidity
                    if t is not None:
                        sys_data["temp"] = round(t, 1)
                        sys_data["hum"] = round(h, 1)
                except Exception:
                    pass
                last_dht_read = time.time()
                
        except Exception:
            pass
            
        time.sleep(0.1)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/data')
def get_data():
    sys_data["manual_state"] = manual_state
    return jsonify(sys_data)

@app.route('/history')
def get_history():
    try:
        conn = sqlite3.connect('sensor_log.db')
        c = conn.cursor()
        c.execute("SELECT time, temperature, humidity FROM dht_data ORDER BY id DESC LIMIT 15")
        rows = c.fetchall()
        conn.close()
        
        rows.reverse()
        labels = [r[0].split(" ")[1][:5] for r in rows] 
        temps = [r[1] for r in rows]
        hums = [r[2] for r in rows]
        return jsonify({"labels": labels, "temps": temps, "hums": hums})
    except Exception:
        return jsonify({"labels": [], "temps": [], "hums": []})

@app.route('/command', methods=['POST'])
def handle_command():
    req = request.get_json()
    action = req.get('action')
    val = req.get('value')
    
    if action == 'mode':
        sys_data["mode"] = val
    elif action == 'toggle' and sys_data["mode"] == 'MANUAL':
        manual_state[val] = not manual_state[val]
        
    return jsonify({"status": "success"})

if __name__ == '__main__':
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)