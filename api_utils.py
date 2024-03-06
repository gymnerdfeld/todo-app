import collections
import datetime
import functools
import inspect
import itertools
import json
import queue
import sys
import threading
import traceback

import werkzeug
import werkzeug.routing
from werkzeug.exceptions import (NotFound, Unauthorized, UnprocessableEntity,
                                 UnsupportedMediaType)
from werkzeug.middleware.dispatcher import DispatcherMiddleware


class API:
    """JSON API.
    >>> app = API()
    >>> @app.GET("/")
    ... def root(request):
    ...     return "Hello World"
    ...
    >>> from werkzeug.test import Client
    >>> client = Client(app)
    >>> response = client.get('/')
    >>> response.get_json()
    'Hello World'

    Now with generic POST data: add a data parameter and optionally specify it's
    type (default is dict)
    >>> @app.POST("/")
    ... def create(request, data:list):
    ...     print(data)
    ...
    >>> response = client.post("/", data="[1, 2, 3]")
    [1, 2, 3]
    >>> response = client.post("/", data="{}")
    >>> response.status
    '422 UNPROCESSABLE ENTITY'

    And finally requesting a dict in the POST data with specified fields (and types)
    >>> @app.PUT("/")
    ... def update(request, name, age:int, superhuman:bool=False):
    ...     print(f"{name} is {age} years old.")
    ...     print(f"{name} is {'' if superhuman else 'not '}superhuman.")
    ...
    >>> import json
    >>> data = {"name": "Betsy", "age": 34}
    >>> response = client.put("/", data=json.dumps(data))
    Betsy is 34 years old.
    Betsy is not superhuman.
    >>> response.status
    '200 OK'
    >>> data = {"name": "Betsy"}
    >>> response = client.put("/", data=json.dumps(data))
    >>> response.status
    '422 UNPROCESSABLE ENTITY'
    >>> data = {"name": "Betsy", "age": "34"}
    >>> response = client.put("/", data=json.dumps(data))
    >>> response.status
    '422 UNPROCESSABLE ENTITY'
    >>> data = {"name": "Betsy", "age": 34, "superhuman": True}
    >>> response = client.put("/", data=json.dumps(data))
    Betsy is 34 years old.
    Betsy is superhuman.
    >>> response.status
    '200 OK'
    >>> data = {"name": "Betsy", "age": 34, "verysmart": True}
    >>> response = client.put("/", data=json.dumps(data))
    >>> response.status
    '422 UNPROCESSABLE ENTITY'
    >>> data = "This is not valid JSON"
    >>> response = client.put("/", data=data)
    >>> response.status
    '415 UNSUPPORTED MEDIA TYPE'
    """

    def __init__(self):
        self._url_map = werkzeug.routing.Map()

    def route(self, string, methods=("GET",), func=None):
        """Register a route with a callback.
        This function can be used either directly:
        >>> api = API()
        >>> api.route("/", func=lambda request: "Hello!")  # doctest: +ELLIPSIS
        <function <lambda> at 0x...>

        or as a decorator
        >>> @api.route("/user/<id>")
        ... def home(request, id):
        ...     return f"Welcome home {id}!"
        ...

        To test it use the Client class.
        >>> from werkzeug.test import Client
        >>> client = Client(api)
        >>> response = client.get("/")
        >>> response.status
        '200 OK'
        >>> response.get_json()
        'Hello!'
        >>> response = client.get("/user/007")
        >>> response.status
        '200 OK'
        >>> response.get_json()
        'Welcome home 007!'
        """
        if func is None:
            return functools.partial(self.route, string, methods)

        rule = werkzeug.routing.Rule(string, methods=methods)
        werkzeug.routing.Map([rule])  # Bind rule temporarily
        url_params = rule.arguments

        sig = inspect.signature(func)
        params = sig.parameters
        param_keys = list(sig.parameters.keys())

        # The first argument is the request, after that, the route parameters follow
        # The order of the parameters is ignored
        func_url_params = set(param_keys[1:len(url_params) + 1])
        missmatch = url_params ^ func_url_params
        if missmatch:
            raise TypeError(
                f"{func.__name__}() arguments and route parameter missmatch "
                f"({func_url_params} != {url_params})"
            )

        body_params = param_keys[len(url_params) + 1:]
        body_type = None
        if len(body_params) == 1 and body_params[0] == "data":
            body_type = (
                params["data"].annotation
                if params["data"].annotation is not inspect.Parameter.empty
                else dict
            )
            content_types = {}
        elif body_params:
            body_type = dict
            content_types = {
                key: params[key].annotation
                for key in body_params
                if params[key].annotation is not inspect.Parameter.empty
            }

        if body_type:
            func = _parse_json_body_wrapper(func, body_type, content_types)

        self._url_map.add(werkzeug.routing.Rule(string, methods=methods, endpoint=func))
        return func

    def GET(self, string):
        """Shorthand for registering GET requests.
        Use as a decorator:
        >>> api = API()
        >>> @api.GET("/admin")
        ... def admin_home(request):
        ...     return "Nothing here"
        ...
        >>> from werkzeug.test import Client
        >>> client = Client(api)
        >>> client.get("/admin")
        <TestResponse streamed [200 OK]>
        """
        return self.route(string, ("GET",))

    def POST(self, string):
        """Shorthand for registering POST requests."""
        return self.route(string, ("POST",))

    def PUT(self, string):
        """Shorthand for registering PUT requests."""
        return self.route(string, ("PUT",))

    def PATCH(self, string):
        """Shorthand for registering PATCH requests."""
        return self.route(string, ("PATCH",))

    def DELETE(self, string):
        """Shorthand for registering DELETE requests."""
        return self.route(string, ("DELETE",))

    def __call__(self, environ, start_response):
        try:
            request = werkzeug.Request(environ)
            adapter = self._url_map.bind_to_environ(environ)
            endpoint, values = adapter.match()

            # Dispatch request
            response = endpoint(request, **values)
            response = _json_response(response)
        except werkzeug.exceptions.HTTPException as e:
            response = _json_response(
                {"code": e.code, "name": e.name, "description": e.description},
                status=e.code,
            )
        except Exception as e:
            response = _json_response(
                {"code": 500, "name": "Internal Server Error"}, status=500
            )
            print(f"ERROR {e.__class__.__name__}: {str(e)}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        return response(environ, start_response)


def _json_response(data, status=200):
    if data is None:
        return werkzeug.Response(status=status)
    elif isinstance(data, werkzeug.Response):
        # already a response
        return data
    else:
        data = json.dumps(data, indent=2) + "\n"
        return werkzeug.Response(data, status=status, mimetype="application/json")


def _parse_json_body_wrapper(func, body_type, content_types):  # noqa: C901
    sig = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(request, *args, **kwargs):
        try:
            data = str(request.data, "utf-8").strip()
        except UnicodeDecodeError:
            raise UnsupportedMediaType("Cannot parse request body: invalid UTF-8 data")

        if not data:
            raise UnsupportedMediaType("Cannot parse request body: no data supplied")

        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            raise UnsupportedMediaType("Cannot parse request body: invalid JSON")

        if not isinstance(data, body_type):
            raise UnprocessableEntity(
                f"Invalid data format: {body_type.__name__} expected"
            )

        if body_type == dict and content_types:
            too_many = data.keys() - sig.parameters.keys()
            if too_many:
                raise UnprocessableEntity(f"Key not allowed: {', '.join(too_many)}")

            kwargs.update(data)
            bound = sig.bind_partial(request, *args, **kwargs)
            bound.apply_defaults()

            missing = sig.parameters.keys() - bound.arguments.keys()
            if missing:
                raise UnprocessableEntity(f"Key missing: {', '.join(missing)}")

            for key, data_type in content_types.items():
                if not isinstance(bound.arguments[key], data_type):
                    raise UnprocessableEntity(
                        f"Invalid format: '{key}' must be of type {data_type.__name__}."
                    )
        else:
            kwargs["data"] = data

        return func(request, *args, **kwargs)

    return wrapper


def timestamp(string=None):
    """Parse JS date strings to datetime objects.

    Returns the current datetime (now), if called with no argument.

    `timestamp` returns UCT timestamps.

    Example usage:

    To convert a JS timestamp, create one in the browser or in node:
    > let now = new Date()
    > JSON.stringify(now)
    '"2020-12-09T23:44:53.782Z"'

    Convert the value to a native Python datetime object:
    >>> timestamp(json.loads('"2020-12-09T23:44:53.782Z"'))
    datetime.datetime(2020, 12, 9, 23, 44, 53, 782000, tzinfo=datetime.timezone.utc)

    To get the current time:
    >>> timestamp()                                         # doctest: +ELLIPSIS
    datetime.datetime(2..., tzinfo=datetime.timezone.utc)

    This function can be used as an annotation in request handlers:
    >>> api = API()
    >>> @api.POST("/reminder")
    ... def reminder(request, date:timestamp, text:str):
    ...     pass
    ...
    """
    if string is not None:
        return datetime.datetime.fromisoformat(string.replace("Z", "+00:00"))
    else:
        return datetime.datetime.now().astimezone(datetime.timezone.utc)


def run(app, prefix=None, port=5000, hostname="127.0.0.1"):
    """Run a wsgi application like an API.

    Optionally specify a listening `port` (default: 3000) and a bind
    `hostname` (default: localhost). Set the hostname to the empty string,
    to listen on all interfaces.
    """
    if prefix is not None:
        app = DispatcherMiddleware(NotFound, {prefix: app})
    werkzeug.run_simple(hostname, port, app, threaded=True, use_reloader=True)


Event = collections.namedtuple("Event", ["id", "event_type", "data"])


class PubSub:
    """Class implementing a publish/subscribe event passing scheme.

    Basic example usage:
    >>> chat = PubSub()
    >>> subscription = chat.subscribe()
    >>> chat.publish("message", "Hello")
    >>> next(subscription)
    Event(id=0, event_type='message', data='Hello')

    Messages can be differentiated by topic:
    >>> general_room = chat.subscribe(topic="general")
    >>> nerd_room = chat.subscribe(topic="nerd")
    >>> chat.publish("new_user", "guido", topic="nerd")
    >>> chat.publish("message", "Hi geeks!", topic="nerd")
    >>> chat.publish("message", "It is 12 am", topic="general")
    >>> next(general_room)
    Event(id=3, event_type='message', data='It is 12 am')
    >>> next(nerd_room)
    Event(id=1, event_type='new_user', data='guido')
    >>> next(nerd_room)
    Event(id=2, event_type='message', data='Hi geeks!')
    """

    def __init__(self):
        self._main_lock = threading.Lock()
        self._topic_locks = collections.defaultdict(threading.Lock)
        self._queues = collections.defaultdict(set)
        self._replay_log = collections.defaultdict(
            lambda: collections.deque(maxlen=1_000)
        )
        # FIXME: not secure?
        self._current_id = itertools.count()

    def publish(self, event_type, data, topic=None):
        """Publish an event.

        The event has an `event_type`, usually a string, and a `data` payload.
        `data` can be free-formed, but should be JSON-serializable.

        Optionally a topic can be specified. The message will be only forwarded
        to subscribers interested in the specified topic.
        """
        with self._main_lock:
            id = next(self._current_id)
            queues = self._queues[topic]
            replay_log = self._replay_log[topic]
            topic_lock = self._topic_locks[topic]

        to_remove = []
        event = Event(id, event_type, data)

        with topic_lock:
            replay_log.append(event)

            for q in queues:
                try:
                    q.put_nowait(event)
                except queue.Full:  # Somebody fell asleep?!?
                    to_remove.append(q)

            for q in to_remove:
                try:
                    queues.remove(q)
                except KeyError:
                    pass

    def broadcast(self, event_type, data):
        """Broadcast event to all subscribers."""
        with self._main_lock:
            topics = list(self._queues.keys())

        for topic in topics:
            self.publish(event_type, data, topic)

    def subscribe(self, topic=None):
        """Subscribe to published events.

        Events are returned as triples, containing a unique event `id`, the
        `event_type`, and the payload `data`.

        Optionally a specific `topic` can be specified
        """
        q = queue.Queue(100)
        with self._main_lock:
            queues = self._queues[topic]
            topic_lock = self._topic_locks[topic]

        with topic_lock:
            queues.add(q)

        def iterator():
            try:
                while q in queues:
                    try:
                        yield q.get(timeout=60)
                    except queue.Empty:
                        pass
            except GeneratorExit:
                try:
                    with topic_lock:
                        queues.remove(q)
                except KeyError:
                    pass

        return iterator()

    def _event_stream(self, replay_events=(), topic=None):
        subscription = self.subscribe(topic)
        for event in itertools.chain(replay_events, subscription):
            yield (
                f"id: {event.id}\n"
                f"event: {event.event_type}\n"
                f"data: {json.dumps(event.data, default=str)}\n\n"
            ).encode("utf-8")

    def _replay_events(self, last_id, topic=None):
        if last_id is None:
            return ()

        last_id = int(last_id)

        with self._main_lock:
            replay_log = self._replay_log[topic]
            topic_lock = self._topic_locks[topic]

        with topic_lock:
            log_iter = iter(replay_log)
            for event in log_iter:
                if event.id == last_id:
                    break
            else:
                raise ValueError(f"{last_id} is not in event log")
            return list(log_iter)

    def streaming_response(self, request, topic=None):
        """Generate a streaming HTTP responses with server-sent events.

        See https://html.spec.whatwg.org/multipage/server-sent-events.html
        for more information about server-sent events.

        When reconnecting after loosing the connection for a while, browsers
        automatically set the `Last-Event-ID` header field to the value of
        the id of the last received event. The response will first replay
        missed events, before sending newly arriving events. When the event
        specified by `Last-Event-ID` is not found, a 404 Not Found response
        is sent, signalling to the browser, that a clean recovery is not
        possible.

        Here is an example session. First, let us create an API:
        >>> api = API()
        >>> chat = PubSub()
        >>> @api.POST("/")
        ... def post_message(request, message:str):
        ...     chat.publish("message", message)
        ...
        >>> @api.GET("/")
        ... def stream(request):
        ...     return chat.streaming_response(request)
        ...

        We can now post messages and see them appear in our subscription:
        >>> subscription = chat.subscribe()
        >>> from werkzeug.test import Client
        >>> client = Client(api)
        >>> resp = client.post("/", json={"message": "hello"})
        >>> resp = client.post("/", json={"message": "everybody"})
        >>> next(subscription)
        Event(id=0, event_type='message', data='hello')
        >>> next(subscription)
        Event(id=1, event_type='message', data='everybody')

        Now, let's simulate a reconnecting browser that only got the first
        message:
        >>> response = client.get("/", headers={"Last-Event-ID": "0"})
        >>> response.status
        '200 OK'

        The events are formatted according to the specification for server-sent
        events:
        >>> i = response.iter_encoded()
        >>> print(str(next(i), encoding="utf-8").strip())
        id: 1
        event: message
        data: "everybody"

        Further incoming messages are sent to the listening client, without
        closing the connection:
        >>> resp = client.post("/", json={"message": "how do you do?"})
        >>> print(str(next(i), encoding="utf-8").strip())
        id: 2
        event: message
        data: "how do you do?"
        """
        last_id = request.headers.get("Last-Event-ID", None)
        try:
            replay_events = self._replay_events(last_id)
        except ValueError:
            raise NotFound()

        return werkzeug.Response(
            self._event_stream(replay_events, topic), mimetype="text/event-stream"
        )


__all__ = (
    "API",
    "NotFound",
    "Unauthorized",
    "UnprocessableEntity",
    "timestamp",
    "run",
    "PubSub",
)