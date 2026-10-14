# Watchover sensörleri

| Platform | Dosya | Durum |
|---|---|---|
| Web (tarayıcı) | `web/rum.js` | Alıcı `/rum.js` adresinden sunar; gerçek Chromium ile uçtan uca test edilir (`tests/test_rum.py`) |
| Android (Kotlin) | `android/WatchoverSensor.kt` | Başvuru kodu; bu depoda derlenmedi (Android SDK yok), uygulamaya eklerken derlenir; ağ ölçümü OkHttp ister |
| iOS (Swift) | `ios/WatchoverSensor.swift` | Başvuru kodu; bu depoda derlenmedi |
| Flutter (Dart) | `flutter/watchover_sensor.dart` | Başvuru kodu; bu depoda derlenmedi, `package:http` ister |
| React Native | `react-native/watchover.js` | Başvuru kodu; sözdizimi Node ile denetlenir |

**Protokol.** `POST <alıcı>/mobile?k=<uygulama anahtarı>` (web: `/rum`), gövde JSON:
`{"session", "platform", "version", "user"?, "device": {"os", "model", "net", "carrier"}, "events": [{"t": "start|view|action|http|error|crash", "ts": <epoch ms>, "name", "dur", "trace", "span", "parent", "method", "url", "status", "msg", "stack", "vitals", "cold"}]}`.
Kullanıcı adımı bir iz başlatır; adımın tetiklediği çağrılara `traceparent: 00-<trace>-<span>-01` başlığı eklenir. Arka uç bu başlığı loglarsa (nginx `$http_traceparent`, uygulama logunda `traceparent` alanı) aynı iz kimlikle birleşir.

**KVKK.** Sensörler form değerlerini, ekran içeriğini, tuş vuruşlarını, konumu ve sorgu dizelerini hiç toplamaz. Sunucu, ne gelirse gelsin, kullanıcı kimliğini kuruluma özel anahtarla takma ada çevirir (ham hali saklanmaz), metinlerde TCKN / telefon / e-posta / IBAN / kart / parola maskeler, adreslerdeki kimlikleri `{id}` yapar ve IP adresini saklamaz.
