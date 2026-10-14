// Watchover mobile sensor for Flutter (reference implementation; depends on package:http).
//
//   await WatchoverSensor.start(endpoint: 'https://watchover.sirket.com.tr/mobile', key: 'pk_...', version: '3.4.1');
//   MaterialApp(navigatorObservers: [WatchoverSensor.observer], ...)      // screen views from routes
//   final client = WatchoverSensor.httpClient();                           // timed calls with a traceparent header
//   WatchoverSensor.setUser(personnelNo);                                   // pseudonymised by the server
//
// Records: app start, route views, calls (method, URL without query, status, duration, traceparent), Flutter and zone errors.
// Never records: text fields, widget content, location, query strings, bodies.
import 'dart:async';
import 'dart:convert';
import 'dart:io' show Platform;
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:http/http.dart' as http;

class WatchoverSensor {
  static final _rnd = Random.secure();
  static final List<Map<String, dynamic>> _queue = [];
  static String _endpoint = '', _key = '', _version = '', _session = _hex(12);
  static String? _user;
  static Map<String, dynamic>? _current;
  static Timer? _timer;
  static final _start = DateTime.now();
  static final NavigatorObserver observer = _Observer();

  static String _hex(int n) => List.generate(n, (_) => _rnd.nextInt(256).toRadixString(16).padLeft(2, '0')).join();
  static int _ms([DateTime? d]) => (d ?? DateTime.now()).millisecondsSinceEpoch;
  static String _platform() => kIsWeb ? 'web' : (Platform.isAndroid ? 'android' : Platform.isIOS ? 'ios' : 'flutter');

  static Future<void> start({required String endpoint, required String key, String version = ''}) async {
    _endpoint = endpoint.replaceAll(RegExp(r'/$'), '');
    _key = key;
    _version = version;
    _push({'t': 'start', 'ts': _ms(_start), 'dur': DateTime.now().difference(_start).inMilliseconds, 'cold': true});
    final prev = FlutterError.onError;
    FlutterError.onError = (details) {
      _push({'t': 'error', 'ts': _ms(), 'msg': details.exceptionAsString().substring(0, min(300, details.exceptionAsString().length)),
             'stack': (details.stack?.toString() ?? '').split('\n').take(15).join('\n')});
      prev?.call(details);
    };
    PlatformDispatcher.instance.onError = (e, st) {
      _push({'t': 'crash', 'ts': _ms(), 'msg': e.toString().substring(0, min(300, e.toString().length)), 'stack': st.toString().split('\n').take(15).join('\n')});
      flush();
      return false;
    };
    _timer ??= Timer.periodic(const Duration(seconds: 10), (_) => flush());
  }

  static void setUser(String? id) => _user = id;

  static void screen(String name) {
    _current = {'trace': _hex(16), 'span': _hex(8), 'start': DateTime.now()};
    _push({'t': 'view', 'ts': _ms(), 'name': name.length > 80 ? name.substring(0, 80) : name, 'trace': _current!['trace'], 'span': _current!['span']});
  }

  static void action(String name) {
    _current = {'trace': _hex(16), 'span': _hex(8), 'start': DateTime.now()};
    final clean = name.replaceAll(RegExp(r'\d{4,}'), '#');
    _push({'t': 'action', 'ts': _ms(), 'name': clean.length > 60 ? clean.substring(0, 60) : clean, 'trace': _current!['trace'], 'span': _current!['span']});
  }

  static http.Client httpClient([http.Client? inner]) => _Client(inner ?? http.Client());

  static void _push(Map<String, dynamic> e) {
    _queue.add(e);
    if (_queue.length >= 30) flush();
  }

  static Future<void> flush() async {
    if (_queue.isEmpty || _endpoint.isEmpty) return;
    final batch = List<Map<String, dynamic>>.from(_queue);
    _queue.clear();
    final body = {'session': _session, 'platform': _platform(), 'version': _version, 'events': batch, if (_user != null) 'user': _user};
    try {
      final r = await http.post(Uri.parse('$_endpoint?k=$_key'), headers: {'Content-Type': 'application/json'}, body: jsonEncode(body));
      if (r.statusCode >= 500) throw Exception('receiver ${r.statusCode}');
    } catch (_) {
      if (_queue.length < 1000) _queue.insertAll(0, batch);
    }
  }
}

class _Observer extends NavigatorObserver {
  @override
  void didPush(Route route, Route? previousRoute) { final n = route.settings.name; if (n != null) WatchoverSensor.screen(n); }
  @override
  void didPop(Route route, Route? previousRoute) { final n = previousRoute?.settings.name; if (n != null) WatchoverSensor.screen(n); }
}

class _Client extends http.BaseClient {
  final http.Client _inner;
  _Client(this._inner);

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    if (request.url.toString().startsWith(WatchoverSensor._endpoint)) return _inner.send(request);
    final c = WatchoverSensor._current;
    final fresh = c != null && DateTime.now().difference(c['start'] as DateTime).inSeconds < 10;
    final trace = fresh ? c!['trace'] as String : WatchoverSensor._hex(16), span = WatchoverSensor._hex(8);
    request.headers['traceparent'] = '00-$trace-$span-01';
    final t0 = DateTime.now();
    int status = 0;
    try {
      final r = await _inner.send(request);
      status = r.statusCode;
      return r;
    } finally {
      WatchoverSensor._push({'t': 'http', 'ts': WatchoverSensor._ms(t0), 'method': request.method, 'url': request.url.replace(query: '').toString().replaceAll('?', ''),
                             'status': status, 'dur': DateTime.now().difference(t0).inMilliseconds, 'trace': trace, 'span': span, if (fresh) 'parent': c!['span']});
    }
  }
}
