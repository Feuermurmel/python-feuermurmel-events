import types, contextlib


def _args_to_str(*args, **kwargs):
	return ', '.join([repr(i) for i in args] + ['%s = %r' % i for i in kwargs.items()])


class EventHandlerException(BaseException):
	"""
	Exception that is thrown if a handler attached to an event throws an exception.
	"""


class NoFailureHandlerException(BaseException):
	"""
	Exception that is thrown if on_failure is called without any event handlers being attached for that event.
	"""


class _Bindable:
	def __init__(self, method : types.FunctionType):
		self._method = method
	
	def __repr__(self):
		return '<{} {}>'.format(type(self).__name__, self._method.__name__)
	
	def bind(self, owner):
		# We bind the event's in the event source's init explicitly, instead of using a descriptor, because we need a single instance per event per event source instance so that we store the listeners in the bound event.
		
		return self.Bound(owner, self._method)
	
	class Bound:
		def __init__(self, owner, method):
			self._owner = owner
			self._method = method
		
		def __repr__(self):
			return '<{} {} of {}>'.format(type(self).__name__, self._method.__name__, self._owner)
		
		def _call_method(self, *args, **kwargs):
			return self._method(self._owner, *args, **kwargs)


class _Event(_Bindable):
	"""
	Decorator for events declared using a method.
	
	Use this decorator for methods in a subclass of BasicTask. When such a class is instantiated, all decorated methods will become callable objects to which handlers for that event can be attached and removed from using the += and -= operators. The body of the method is run with the same arguments before attached handlers are called.
	"""
	
	class Bound(_Bindable.Bound):
		def __init__(self, owner, method):
			super().__init__(owner, method)
			
			self._subscribers = []
		
		def __call__(self, *args, **kwargs):
			def do_call():
				# We make a copy before iteration because it should be possible to add or remove event handlers from a handler for the same event.
				for i in list(self._subscribers):
					try:
						i(*args, **kwargs)
					except Exception as e:
						raise EventHandlerException('Exception when calling event handler {}.'.format(i)) from e
			
			if self._owner._running:
				self._call_method(*args, **kwargs)
				self._owner._run_within_transaction(do_call)
		
		def __iadd__(self, handler):
			assert callable(handler)
			
			self._subscribers.append(handler)
			
			return self
		
		def __isub__(self, handler):
			self._subscribers.remove(handler)
			
			return self


class _Transaction(_Bindable):
	"""
	Decorator for methods that may signal events but want them the be queued until the method exist.
	
	Use this method in subclasses of BasicTask.
	"""
	
	class Bound(_Bindable.Bound):
		def __call__(self, *args, **kwargs):
			with self._owner._transaction_context():
				self._call_method(*args, **kwargs)


event = _Event
transaction = _Transaction


class BasicTask:
	"""Base class for all types of tasks.
	
	This class only supports declaring event methods in a subclass' definition."""
	
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		
		# We declare this member here as it is needed by events even if they're attached to a task
		self._running = True
		
		# Set to a list while events are being queued. The list contains callable that should be called when the transaction ends.
		self._event_signal_queue = None

		cls = type(self)
		
		for name in dir(cls):
			value = getattr(cls, name)
			
			if isinstance(value, _Bindable):
				# Copy the method to the instance.
				setattr(self, name, value.bind(self))
	
	@contextlib.contextmanager
	def _transaction_context(self):
		if self._event_signal_queue is None:
			self._event_signal_queue = queue = []
			
			try:
				yield queue
			finally:
				self._event_signal_queue = None
			
			# Not firing events when an exception was thrown inside a transaction 
			for i in queue:
				i()
		else:
			yield self._event_signal_queue
	
	def _run_within_transaction(self, fn):
		"""Run the given callable at the end of the current transaction.
		
		If no transaction is currently running, the callable will be called immediately."""
		
		with self._transaction_context() as queue:
			queue.append(fn)


class Task(BasicTask):
	"""
	Task that can be stopped.
	"""
	
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		
		# This is set to an exception instance when this event source fails.
		self._exception = None
		self._adopted_tasks = []
	
	def _cleanup(self):
		"""Override this method to clean up any state associated with this object."""
	
	def _adopt_task(self, task, failure_handler = None):
		if failure_handler is None:
			failure_handler = self.on_failure
		
		self._adopted_tasks.append(task)
		task.on_failure += failure_handler
	
	def stop(self):
		if not self._running:
			raise Exception('stop() called on non-running task.')
		
		self._running = False
		self._cleanup()
		
		for i in self._adopted_tasks:
			stop_task(i)
	
	@property
	def running(self):
		return self._running
	
	@property
	def failed(self):
		assert not self._running
		
		return self._exception is not None
	
	@property
	def exception(self):
		assert self._exception is not None
		
		return self._exception
	
	@event
	def on_failure(self, exception):
		if not self.on_failure._subscribers:
			raise NoFailureHandlerException()
		
		self._exception = exception
		
		self.stop()
	
	@classmethod
	def create_failure(cls, exception):
		"""Return a failed task."""
		
		res = cls()
		res.on_failure(exception)
		
		return res


class Future(Task):
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		
		self._result = None
		self._succeeded = False
	
	@property
	def succeeded(self):
		assert not self._running
		
		return self._succeeded
	
	@property
	def result(self):
		assert self._succeeded
		
		return self._result
	
	@event
	def on_success(self, result):
		self._result = result
		self._succeeded = True
		
		self.stop()
	
	@classmethod
	def create_success(cls, result = None):
		"""Return a succeeded future."""
		
		res = cls()
		res.on_success(result)
		
		return res


def stop_task(event_source : (Task, None)):
	"""Can be called with any event source or None. When the argument is a running event source, stop it, otherwise, do nothing."""
	
	if event_source is not None and event_source.running:
		event_source.stop()
