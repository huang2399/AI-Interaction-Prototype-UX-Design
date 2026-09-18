import urllib.request, json, time

def trigger():
    req = urllib.request.Request('http://10.1.41.53:5000/api/trigger_capture', method='POST')
    return urllib.request.urlopen(req).read().decode()

def status():
    r = urllib.request.urlopen('http://10.1.41.53:5000/api/status')
    return json.loads(r.read())

print('=== 第1次点击 ===')
print('Click:', trigger())
time.sleep(2)
print('=== 第2次点击 ===')
print('Click:', trigger())
time.sleep(2)
print('=== 第3次点击 ===')
print('Click:', trigger())

print()
print('=== 等待拍照完成，每3秒检查一次 ===')
for i in range(1, 25):
    s = status()
    print(f'  [{i*3}s] Status={s["status"]}, Photo={s.get("latest_photo","-")}')
    if s['status'] == 'IDLE' or s['status'] == 'COMPLETED':
        if s['status'] == 'IDLE':
            print('  >>> All tasks completed!')
            break
        # COMPLETED but maybe more tasks queued
    time.sleep(3)
else:
    print('  >>> Timeout!')

print()
print('=== Final Status ===')
print(json.dumps(status(), indent=2))

# Check photos
final = status()
if final.get('latest_photo'):
    import os
    print(f'\n=== Check photo exists under UPLOAD_FOLDER ===')
    # Try to access photo
    r = urllib.request.urlopen(f'http://10.1.41.53:5000/photos/{final["latest_photo"]}')
    print(f'Photo URL OK: {len(r.read())} bytes')