# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Python wrappers for Datasets and Iterators."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import collections
import threading

import numpy as np

from tensorflow.python.data.util import nest
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import function
from tensorflow.python.framework import ops
from tensorflow.python.framework import random_seed
from tensorflow.python.framework import sparse_tensor as sparse_tensor_lib
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_util
from tensorflow.python.ops import gen_dataset_ops
from tensorflow.python.ops import gen_io_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import script_ops


class Iterator(object):
  """Represents the state of iterating through a `Dataset`."""

  def __init__(self, iterator_resource, initializer, output_types,
               output_shapes):
    """Creates a new iterator from the given iterator resource.

    NOTE(mrry): Most users will not call this initializer directly, and will
    instead use `Iterator.from_dataset()` or `Dataset.make_one_shot_iterator()`.

    Args:
      iterator_resource: A `tf.resource` scalar `tf.Tensor` representing the
        iterator.
      initializer: A `tf.Operation` that should be run to initialize this
        iterator.
      output_types: A nested structure of `tf.DType` objects corresponding to
        each component of an element of this iterator.
      output_shapes: A nested structure of `tf.TensorShape` objects
        corresponding to each component of an element of this dataset.
    """
    self._iterator_resource = iterator_resource
    self._initializer = initializer
    self._output_types = output_types
    self._output_shapes = output_shapes

  @staticmethod
  def from_dataset(dataset, shared_name=None):
    """Creates a new, uninitialized `Iterator` from the given `Dataset`.

    To initialize this iterator, you must run its `initializer`:

    ```python
    dataset = ...
    iterator = Iterator.from_dataset(dataset)
    # ...
    sess.run(iterator.initializer)
    ```

    Args:
      dataset: A `Dataset` object.
      shared_name: (Optional.) If non-empty, this iterator will be shared under
        the given name across multiple sessions that share the same devices
        (e.g. when using a remote server).

    Returns:
      An `Iterator`.
    """
    if shared_name is None:
      shared_name = ""
    iterator_resource = gen_dataset_ops.iterator(
        container="",
        shared_name=shared_name,
        output_types=nest.flatten(dataset.output_types),
        output_shapes=nest.flatten(dataset.output_shapes))
    with ops.colocate_with(iterator_resource):
      initializer = gen_dataset_ops.make_iterator(
          dataset.make_dataset_resource(), iterator_resource)
    return Iterator(iterator_resource, initializer, dataset.output_types,
                    dataset.output_shapes)

  @staticmethod
  def from_structure(output_types, output_shapes=None, shared_name=None):
    """Creates a new, uninitialized `Iterator` with the given structure.

    This iterator-constructing method can be used to create an iterator that
    is reusable with many different datasets.

    The returned iterator is not bound to a particular dataset, and it has
    no `initializer`. To initialize the iterator, run the operation returned by
    `Iterator.make_initializer(dataset)`.

    The following is an example

    ```python
    iterator = Iterator.from_structure(tf.int64, tf.TensorShape([]))

    dataset_range = Dataset.range(10)
    range_initializer = iterator.make_initializer(dataset_range)

    dataset_evens = dataset_range.filter(lambda x: x % 2 == 0)
    evens_initializer = iterator.make_initializer(dataset_evens)

    # Define a model based on the iterator; in this example, the model_fn
    # is expected to take scalar tf.int64 Tensors as input (see
    # the definition of 'iterator' above).
    prediction, loss = model_fn(iterator.get_next())

    # Train for `num_epochs`, where for each epoch, we first iterate over
    # dataset_range, and then iterate over dataset_evens.
    for _ in range(num_epochs):
      # Initialize the iterator to `dataset_range`
      sess.run(range_initializer)
      while True:
        try:
          pred, loss_val = sess.run([prediction, loss])
        except tf.errors.OutOfRangeError:
          break

      # Initialize the iterator to `dataset_evens`
      sess.run(evens_initializer)
      while True:
        try:
          pred, loss_val = sess.run([prediction, loss])
        except tf.errors.OutOfRangeError:
          break
    ```

    Args:
      output_types: A nested structure of `tf.DType` objects corresponding to
        each component of an element of this iterator.
      output_shapes: (Optional.) A nested structure of `tf.TensorShape` objects
        corresponding to each component of an element of this dataset. If
        omitted, each component will have an unconstrainted shape.
      shared_name: (Optional.) If non-empty, this iterator will be shared under
        the given name across multiple sessions that share the same devices
        (e.g. when using a remote server).

    Returns:
      An `Iterator`.

    Raises:
      TypeError: If the structures of `output_shapes` and `output_types` are
        not the same.
    """
    output_types = nest.map_structure(dtypes.as_dtype, output_types)
    if output_shapes is None:
      output_shapes = nest.map_structure(
          lambda _: tensor_shape.TensorShape(None), output_types)
    else:
      output_shapes = nest.map_structure_up_to(
          output_types, tensor_shape.as_shape, output_shapes)
    nest.assert_same_structure(output_types, output_shapes)
    if shared_name is None:
      shared_name = ""
    iterator_resource = gen_dataset_ops.iterator(
        container="",
        shared_name=shared_name,
        output_types=nest.flatten(output_types),
        output_shapes=nest.flatten(output_shapes))
    return Iterator(iterator_resource, None, output_types, output_shapes)

  @staticmethod
  def from_string_handle(string_handle, output_types, output_shapes=None):
    """Creates a new, uninitialized `Iterator` based on the given handle.

    This method allows you to define a "feedable" iterator where you can choose
    between concrete iterators by feeding a value in a @{tf.Session.run} call.
    In that case, `string_handle` would a @{tf.placeholder}, and you would feed
    it with the value of @{tf.contrib.data.Iterator.string_handle} in each step.

    For example, if you had two iterators that marked the current position in
    a training dataset and a test dataset, you could choose which to use in
    each step as follows:

    ```python
    train_iterator = tf.contrib.data.Dataset(...).make_one_shot_iterator()
    train_iterator_handle = sess.run(train_iterator.string_handle())

    test_iterator = tf.contrib.data.Dataset(...).make_one_shot_iterator()
    test_iterator_handle = sess.run(test_iterator.string_handle())

    handle = tf.placeholder(tf.string, shape=[])
    iterator = tf.contrib.data.Iterator.from_string_handle(
        handle, train_iterator.output_types)

    next_element = iterator.get_next()
    loss = f(next_element)

    train_loss = sess.run(loss, feed_dict={handle: train_iterator_handle})
    test_loss = sess.run(loss, feed_dict={handle: test_iterator_handle})
    ```

    Args:
      string_handle: A scalar `tf.Tensor` of type `tf.string` that evaluates
        to a handle produced by the `Iterator.string_handle()` method.
      output_types: A nested structure of `tf.DType` objects corresponding to
        each component of an element of this iterator.
      output_shapes: (Optional.) A nested structure of `tf.TensorShape` objects
        corresponding to each component of an element of this dataset. If
        omitted, each component will have an unconstrainted shape.

    Returns:
      An `Iterator`.
    """
    output_types = nest.map_structure(dtypes.as_dtype, output_types)
    if output_shapes is None:
      output_shapes = nest.map_structure(
          lambda _: tensor_shape.TensorShape(None), output_types)
    else:
      output_shapes = nest.map_structure_up_to(
          output_types, tensor_shape.as_shape, output_shapes)
    nest.assert_same_structure(output_types, output_shapes)
    string_handle = ops.convert_to_tensor(string_handle, dtype=dtypes.string)
    iterator_resource = gen_dataset_ops.iterator_from_string_handle(
        string_handle,
        output_types=nest.flatten(output_types),
        output_shapes=nest.flatten(output_shapes))
    return Iterator(iterator_resource, None, output_types, output_shapes)

  @property
  def initializer(self):
    """A `tf.Operation` that should be run to initialize this iterator.

    Returns:
      A `tf.Operation` that should be run to initialize this iterator

    Raises:
      ValueError: If this iterator initializes itself automatically.
    """
    if self._initializer is not None:
      return self._initializer
    else:
      # TODO(mrry): Consider whether one-shot iterators should have
      # initializers that simply reset their state to the beginning.
      raise ValueError("Iterator does not have an initializer.")

  def make_initializer(self, dataset, name=None):
    """Returns a `tf.Operation` that initializes this iterator on `dataset`.

    Args:
      dataset: A `Dataset` with compatible structure to this iterator.
      name: (Optional.) A name for the created operation.

    Returns:
      A `tf.Operation` that can be run to initialize this iterator on the given
      `dataset`.

    Raises:
      TypeError: If `dataset` and this iterator do not have a compatible
        element structure.
    """
    with ops.name_scope(name, "make_initializer") as name:
      nest.assert_same_structure(self._output_types, dataset.output_types)
      nest.assert_same_structure(self._output_shapes, dataset.output_shapes)
      for iterator_dtype, dataset_dtype in zip(
          nest.flatten(self._output_types), nest.flatten(dataset.output_types)):
        if iterator_dtype != dataset_dtype:
          raise TypeError(
              "Expected output types %r but got dataset with output types %r." %
              (self._output_types, dataset.output_types))
      for iterator_shape, dataset_shape in zip(
          nest.flatten(self._output_shapes),
          nest.flatten(dataset.output_shapes)):
        if not iterator_shape.is_compatible_with(dataset_shape):
          raise TypeError("Expected output shapes compatible with %r but got "
                          "dataset with output shapes %r." %
                          (self._output_shapes, dataset.output_shapes))
    with ops.colocate_with(self._iterator_resource):
      return gen_dataset_ops.make_iterator(
          dataset.make_dataset_resource(), self._iterator_resource, name=name)

  def get_next(self, name=None):
    """Returns a nested structure of `tf.Tensor`s containing the next element.

    Args:
      name: (Optional.) A name for the created operation.

    Returns:
      A nested structure of `tf.Tensor` objects.
    """
    return nest.pack_sequence_as(
        self._output_types,
        gen_dataset_ops.iterator_get_next(
            self._iterator_resource,
            output_types=nest.flatten(self._output_types),
            output_shapes=nest.flatten(self._output_shapes),
            name=name))

  def dispose_op(self, name=None):
    """Returns a `tf.Operation` that destroys this iterator.

    The returned operation may be used to release any resources consumed by
    this iterator without closing the session.

    Args:
      name: (Optional.) A name for the created operation.

    Returns:
      A `tf.Operation`.
    """
    return gen_dataset_ops.iterator_dispose(self._iterator_resource, name=name)

  def string_handle(self, name=None):
    """Returns a string-valued `tf.Tensor` that represents this iterator.

    Args:
      name: (Optional.) A name for the created operation.

    Returns:
      A scalar `tf.Tensor` of type `tf.string`.
    """
    return gen_dataset_ops.iterator_to_string_handle(
        self._iterator_resource, name=name)

  @property
  def output_shapes(self):
    """Returns the shape of each component of an element of this iterator.

    Returns:
      A nested structure of `tf.TensorShape` objects corresponding to each
      component of an element of this iterator.
    """
    return self._output_shapes

  @property
  def output_types(self):
    """Returns the type of each component of an element of this iterator.

    Returns:
      A nested structure of `tf.DType` objects corresponding to each component
      of an element of this iterator.
    """
    return self._output_types


class Dataset(object):
  """Represents a potentially large set of elements.

  A `Dataset` can be used to represent an input pipeline as a
  collection of elements (nested structures of tensors) and a "logical
  plan" of transformations that act on those elements.
  """
  __metaclass__ = abc.ABCMeta

  def __init__(self):
    pass

  # TODO(mrry): Rename this to `make_dataset_variant()`,
  # `make_dataset_tensor()`, or something else more accurate.
  @abc.abstractmethod
  def make_dataset_resource(self):
    """Creates a scalar `tf.Tensor` of `tf.variant` representing this dataset.

    Returns:
      A scalar `tf.Tensor` of `tf.variant` type, which represents this dataset.
    """
    raise NotImplementedError("Dataset.make_dataset_resource")

  def make_initializable_iterator(self, shared_name=None):
    """Creates an `Iterator` for enumerating the elements of this dataset.

    **N.B.** The returned iterator will be in an uninitialized state,
    and you must run the `iterator.initializer` operation before using it.

    Args:
      shared_name: (Optional.) If non-empty, this iterator will be shared under
        the given name across multiple sessions that share the same devices
        (e.g. when using a remote server).


    Returns:
      An `Iterator` over the elements of this dataset.
    """
    return Iterator.from_dataset(self, shared_name)

  def make_one_shot_iterator(self):
    """Creates an `Iterator` for enumerating the elements of this dataset.

    **N.B.** The returned iterator will be initialized automatically.
    A "one-shot" iterator does not currently support re-initialization.

    Returns:
      An `Iterator` over the elements of this dataset.
    """
    # NOTE(mrry): We capture by value here to ensure that `_make_dataset()` is
    # a 0-argument function.
    @function.Defun(capture_by_value=True)
    def _make_dataset():
      return self.make_dataset_resource()

    _make_dataset.add_to_graph(ops.get_default_graph())

    return Iterator(
        gen_dataset_ops.one_shot_iterator(
            dataset_factory=_make_dataset,
            output_types=nest.flatten(self.output_types),
            output_shapes=nest.flatten(self.output_shapes)), None,
        self.output_types, self.output_shapes)

  @abc.abstractproperty
  def output_shapes(self):
    """Returns the shape of each component of an element of this dataset.

    Returns:
      A nested structure of `tf.TensorShape` objects corresponding to each
      component of an element of this dataset.
    """
    raise NotImplementedError("Dataset.output_shapes")

  @abc.abstractproperty
  def output_types(self):
    """Returns the type of each component of an element of this dataset.

    Returns:
      A nested structure of `tf.DType` objects corresponding to each component
      of an element of this dataset.
    """
    raise NotImplementedError("Dataset.output_types")

  def __repr__(self):
    output_shapes = nest.map_structure(str, self.output_shapes)
    output_shapes = str(output_shapes).replace("'", "")
    output_types = nest.map_structure(repr, self.output_types)
    output_types = str(output_types).replace("'", "")
    return ("<%s shapes: %s, types: %s>" % (type(self).__name__, output_shapes,
                                            output_types))

  @staticmethod
  def from_tensors(tensors):
    """Creates a `Dataset` with a single element, comprising the given tensors.

    Args:
      tensors: A nested structure of tensors.

    Returns:
      A `Dataset`.
    """
    return TensorDataset(tensors)

  @staticmethod
  def from_tensor_slices(tensors):
    """Creates a `Dataset` whose elements are slices of the given tensors.

    Args:
      tensors: A nested structure of tensors, each having the same size in the
        0th dimension.

    Returns:
      A `Dataset`.
    """
    return TensorSliceDataset(tensors)

  @staticmethod
  def from_sparse_tensor_slices(sparse_tensor):
    """Splits each rank-N `tf.SparseTensor` in this dataset row-wise.

    Args:
      sparse_tensor: A `tf.SparseTensor`.

    Returns:
      A `Dataset` of rank-(N-1) sparse tensors.
    """
    return SparseTensorSliceDataset(sparse_tensor)

  class _GeneratorState(object):
    """Stores outstanding iterators created from a Python generator.

    This class keeps track of potentially multiple iterators that may have
    been created from a generator, e.g. in the case that the dataset is
    repeated, or nested within a parallel computation.
    """

    def __init__(self, generator):
      self._generator = generator
      self._lock = threading.Lock()
      self._next_id = 0  # GUARDED_BY(self._lock)
      self._iterators = collections.defaultdict(lambda: iter(generator()))

    def get_next_id(self):
      with self._lock:
        ret = self._next_id
        self._next_id += 1
      return ret

    def get_iterator(self, iterator_id):
      return self._iterators[iterator_id]

    def iterator_completed(self, iterator_id):
      del self._iterators[iterator_id]

  @staticmethod
  def from_generator(generator, output_types, output_shapes=None):
    """Creates a `Dataset` whose elements are generated by `generator`.

    The `generator` argument must be a callable object that returns
    an object that support the `iter()` protocol (e.g. a generator function).
    The elements generated by `generator` must be compatible with the given
    `output_types` and (optional) `output_shapes` arguments.

    For example:

    ```python
    import itertools

    def gen():
      for i in itertools.count(1):
        yield (i, [1] * i)

    ds = Dataset.from_generator(
        gen, (tf.int64, tf.int64), (tf.TensorShape([]), tf.TensorShape([None])))
    value = ds.make_one_shot_iterator().get_next()

    sess.run(value)  # (1, array([1]))
    sess.run(value)  # (2, array([1, 1]))
    ```

    Args:
      generator: A callable object that takes no arguments and returns an
        object that supports the `iter()` protocol.
      output_types: A nested structure of `tf.DType` objects corresponding to
        each component of an element yielded by `generator`.
      output_shapes: (Optional.) A nested structure of `tf.TensorShape`
        objects corresponding to each component of an element yielded by
        `generator`.

    Returns:
      A `Dataset`.
    """
    if not callable(generator):
      raise TypeError("`generator` must be callable.")
    if output_shapes is None:
      output_shapes = nest.map_structure(
          lambda _: tensor_shape.TensorShape(None), output_types)
    else:
      output_shapes = nest.map_structure_up_to(
          output_types, tensor_shape.as_shape, output_shapes)

    flattened_types = nest.flatten(output_types)
    flattened_shapes = nest.flatten(output_shapes)

    generator_state = Dataset._GeneratorState(generator)

    def get_iterator_id_map_fn(unused_dummy):
      """Creates a unique `iterator_id` for each pass over the dataset.

      The "iterator_id" disambiguates between multiple concurrently
      existing iterators.

      Args:
        unused_dummy: Ignored value.

      Returns:
        A `tf.int64` tensor whose value uniquely identifies an iterator in
        `generator_state`.
      """
      return script_ops.py_func(
          generator_state.get_next_id, [], dtypes.int64, stateful=True)

    def generator_map_fn(iterator_id_t):
      """Generates the next element from iterator with ID `iterator_id_t`.

      We map this function across an infinite repetition of the
      `iterator_id_t`, and raise `StopIteration` to terminate the iteration.

      Args:
        iterator_id_t: A `tf.int64` tensor whose value uniquely identifies
          the iterator in `generator_state` from which to generate an element.

      Returns:
        A nested structure of tensors representing an element from the iterator.
      """

      def generator_py_func(iterator_id):
        """A `py_func` that will be called to invoke the iterator."""
        try:
          values = next(generator_state.get_iterator(iterator_id))
        except StopIteration:
          generator_state.iterator_completed(iterator_id)
          raise StopIteration("Iteration finished.")

        # Use the same _convert function from the py_func() implementation to
        # convert the returned values to arrays early, so that we can inspect
        # their values.
        # pylint: disable=protected-access
        ret_arrays = [
            script_ops.FuncRegistry._convert(ret)
            for ret in nest.flatten_up_to(output_types, values)
        ]
        # pylint: enable=protected-access

        # Additional type and shape checking to ensure that the components
        # of the generated element match the `output_types` and `output_shapes`
        # arguments.
        for (ret_array, expected_dtype, expected_shape) in zip(
            ret_arrays, flattened_types, flattened_shapes):
          if ret_array.dtype != expected_dtype.as_numpy_dtype:
            raise TypeError(
                "`generator` yielded an element of type %s where an element "
                "of type %s was expected." % (ret_array.dtype,
                                              expected_dtype.as_numpy_dtype))
          if not expected_shape.is_compatible_with(ret_array.shape):
            raise ValueError(
                "`generator` yielded an element of shape %s where an element "
                "of shape %s was expected." % (ret_array.shape, expected_shape))

        return ret_arrays

      flat_values = script_ops.py_func(
          generator_py_func, [iterator_id_t], flattened_types, stateful=True)

      # The `py_func()` op drops the inferred shapes, so we add them back in
      # here.
      if output_shapes is not None:
        for ret_t, shape in zip(flat_values, flattened_shapes):
          ret_t.set_shape(shape)

      return nest.pack_sequence_as(output_types, flat_values)

    # This function associates each traversal of `generator` with a unique
    # iterator ID.
    def flat_map_fn(iterator_id_t):
      # First, generate an infinite dataset containing the iterator ID repeated
      # forever.
      repeated_id = Dataset.from_tensors(iterator_id_t).repeat(None)

      # The `generator_map_fn` gets the next element from the iterator with the
      # relevant ID, and raises StopIteration when that iterator contains no
      # more elements.
      return repeated_id.map(generator_map_fn)

    # A single-element dataset that, each time it is evaluated, contains a
    # freshly-generated and unique (for the returned dataset) int64
    # ID that will be used to identify the appropriate Python state, which
    # is encapsulated in `generator_state`, and captured in
    # `get_iterator_id_map_fn`.
    dummy = 0
    id_dataset = Dataset.from_tensors(dummy).map(get_iterator_id_map_fn)

    # A dataset that contains all of the elements generated by a
    # single iterator created from `generator`, identified by the
    # iterator ID contained in `id_dataset`. Lifting the iteration
    # into a flat_map here enables multiple repetitions and/or nested
    # versions of the returned dataset to be created, because it forces
    # the generation of a new ID for each version.
    return id_dataset.flat_map(flat_map_fn)

  @staticmethod
  def range(*args):
    """Creates a `Dataset` of a step-separated range of values.

    For example:

    ```python
    Dataset.range(5) == [0, 1, 2, 3, 4]
    Dataset.range(2, 5) == [2, 3, 4]
    Dataset.range(1, 5, 2) == [1, 3]
    Dataset.range(1, 5, -2) == []
    Dataset.range(5, 1) == []
    Dataset.range(5, 1, -2) == [5, 3]
    ```

    Args:
      *args: follow same semantics as python's xrange.
        len(args) == 1 -> start = 0, stop = args[0], step = 1
        len(args) == 2 -> start = args[0], stop = args[1], step = 1
        len(args) == 3 -> start = args[0], stop = args[1, stop = args[2]

    Returns:
      A `RangeDataset`.

    Raises:
      ValueError: if len(args) == 0.
    """
    return RangeDataset(*args)

  @staticmethod
  def zip(datasets):
    """Creates a `Dataset` by zipping together the given datasets.

    This method has similar semantics to the built-in `zip()` function
    in Python, with the main difference being that the `datasets`
    argument can be an arbitrary nested structure of `Dataset` objects.
    For example:

    ```python
    # NOTE: The following examples use `{ ... }` to represent the
    # contents of a dataset.
    a = { 1, 2, 3 }
    b = { 4, 5, 6 }
    c = { (7, 8), (9, 10), (11, 12) }
    d = { 13, 14 }

    # The nested structure of the `datasets` argument determines the
    # structure of elements in the resulting dataset.
    Dataset.zip((a, b)) == { (1, 4), (2, 5), (3, 6) }
    Dataset.zip((b, a)) == { (4, 1), (5, 2), (6, 3) }

    # The `datasets` argument may contain an arbitrary number of
    # datasets.
    Dataset.zip((a, b, c)) == { (1, 4, (7, 8)),
                                (2, 5, (9, 10)),
                                (3, 6, (11, 12)) }

    # The number of elements in the resulting dataset is the same as
    # the size of the smallest dataset in `datasets`.
    Dataset.zip((a, d)) == { (1, 13), (2, 14) }
    ```

    Args:
      datasets: A nested structure of datasets.

    Returns:
      A `Dataset`.
    """
    return ZipDataset(datasets)

  def concatenate(self, dataset):
    """Creates a `Dataset` by concatenating given dataset with this dataset.

    ```python
    # NOTE: The following examples use `{ ... }` to represent the
    # contents of a dataset.
    a = { 1, 2, 3 }
    b = { 4, 5, 6, 7 }

    # Input dataset and dataset to be concatenated should have same
    # nested structures and output types.
    # c = { (8, 9), (10, 11), (12, 13) }
    # d = { 14.0, 15.0, 16.0 }
    # a.concatenate(c) and a.concatenate(d) would result in error.

    a.concatenate(b) == { 1, 2, 3, 4, 5, 6, 7 }
    ```

    Args:
      dataset: `Dataset` to be concatenated.

    Returns:
      A `Dataset`.
    """
    return ConcatenateDataset(self, dataset)

  def prefetch(self, buffer_size):
    """Creates a `Dataset` that prefetches elements from this dataset.

    Args:
      buffer_size: A `tf.int64` scalar `tf.Tensor`, representing the
        maximum number elements that will be buffered when prefetching.

    Returns:
      A `Dataset`.
    """
    return PrefetchDataset(self, buffer_size)

  @staticmethod
  def list_files(file_pattern):
    """A dataset of all files matching a pattern.

    Example:
      If we had the following files on our filesystem:
        - /path/to/dir/a.txt
        - /path/to/dir/b.py
        - /path/to/dir/c.py
      If we pass "/path/to/dir/*.py" as the directory, the dataset would
      produce:
        - /path/to/dir/b.py
        - /path/to/dir/c.py

    Args:
      file_pattern: A string or scalar string `tf.Tensor`, representing
        the filename pattern that will be matched.

    Returns:
     A `Dataset` of strings corresponding to file names.
    """
    return Dataset.from_tensor_slices(gen_io_ops.matching_files(file_pattern))

  def repeat(self, count=None):
    """Repeats this dataset `count` times.

    Args:
      count: (Optional.) A `tf.int64` scalar `tf.Tensor`, representing the
        number of times the elements of this dataset should be repeated. The
        default behavior (if `count` is `None` or `-1`) is for the elements to
        be repeated indefinitely.

    Returns:
      A `Dataset`.
    """
    return RepeatDataset(self, count)

  def _enumerate(self, start=0):

    max_value = np.iinfo(dtypes.int64.as_numpy_dtype).max
    return Dataset.zip((Dataset.range(start, max_value), self))

  def shuffle(self, buffer_size, seed=None):
    """Randomly shuffles the elements of this dataset.

    Args:
      buffer_size: A `tf.int64` scalar `tf.Tensor`, representing the
        number of elements from this dataset from which the new
        dataset will sample.
      seed: (Optional.) A `tf.int64` scalar `tf.Tensor`, representing the
        random seed that will be used to create the distribution. See
        @{tf.set_random_seed} for behavior.

    Returns:
      A `Dataset`.
    """
    return ShuffleDataset(self, buffer_size, seed)

  def cache(self, filename=""):
    """Caches the elements in this dataset.

    Args:
      filename: A `tf.string` scalar `tf.Tensor`, representing the name of a
        directory on the filesystem to use for caching tensors in this Dataset.
        If a filename is not provided, the dataset will be cached in memory.

    Returns:
      A `Dataset`.
    """
    return CacheDataset(self, filename)

  def take(self, count):
    """Creates a `Dataset` with at most `count` elements from this dataset.

    Args:
      count: A `tf.int64` scalar `tf.Tensor`, representing the number of
        elements of this dataset that should be taken to form the new dataset.
        If `count` is -1, or if `count` is greater than the size of this
        dataset, the new dataset will contain all elements of this dataset.

    Returns:
      A `Dataset`.
    """
    return TakeDataset(self, count)

  def skip(self, count):
    """Creates a `Dataset` that skips `count` elements from this dataset.

    Args:
      count: A `tf.int64` scalar `tf.Tensor`, representing the number
        of elements of this dataset that should be skipped to form the
        new dataset.  If `count` is greater than the size of this
        dataset, the new dataset will contain no elements.  If `count`
        is -1, skips the entire dataset.

    Returns:
      A `Dataset`.
    """
    return SkipDataset(self, count)

  def shard(self, num_shards, index):
    """Creates a `Dataset` that includes only 1/`num_shards` of this dataset.

    This dataset operator is very useful when running distributed training, as
    it allows each worker to read a unique subset.

    When reading a single input file, you can skip elements as follows:

    ```python
    d = tf.data.TFRecordDataset(FLAGS.input_file)
    d = d.shard(FLAGS.num_workers, FLAGS.worker_index)
    d = d.repeat(FLAGS.num_epochs)
    d = d.shuffle(FLAGS.shuffle_buffer_size)
    d = d.map(parser_fn, num_parallel_calls=FLAGS.num_map_threads)
    ```

    Important caveats:

    - Be sure to shard before you use any randomizing operator (such as
      shuffle).
    - Generally it is best if the shard operator is used early in the dataset
      pipeline. For example, when reading from a set of TFRecord files, shard
      before converting the dataset to input samples. This avoids reading every
      file on every worker. The following is an example of an efficient
      sharding strategy within a complete pipeline:

    ```python
    d = Dataset.list_files(FLAGS.pattern)
    d = d.shard(FLAGS.num_workers, FLAGS.worker_index)
    d = d.repeat(FLAGS.num_epochs)
    d = d.shuffle(FLAGS.shuffle_buffer_size)
    d = d.repeat()
    d = d.interleave(tf.data.TFRecordDataset,
                     cycle_length=FLAGS.num_readers, block_length=1)
    d = d.map(parser_fn, num_parallel_calls=FLAGS.num_map_threads)
    ```

    Args:
      num_shards: A `tf.int64` scalar `tf.Tensor`, representing the number of
        shards operating in parallel.
      index: A `tf.int64` scalar `tf.Tensor`, representing the worker index.

    Returns:
      A `Dataset`.

    Raises:
      ValueError: if `num_shards` or `index` are illegal values. Note: error
        checking is done on a best-effort basis, and aren't guaranteed to be
        caught upon dataset creation. (e.g. providing in a placeholder tensor
        bypasses the early checking, and will instead result in an error during
        a session.run call.)
    """
    num_shards = ops.convert_to_tensor(
        num_shards, name="num_shards", dtype=dtypes.int64)
    num_shards_static = tensor_util.constant_value(num_shards)
    index = ops.convert_to_tensor(index, name="index", dtype=dtypes.int64)
    index_static = tensor_util.constant_value(index)

    if num_shards_static is not None and num_shards_static < 1:
      raise ValueError("num_shards must be >= 1; got: %s" % num_shards_static)
    if index_static is not None and index_static < 0:
      raise ValueError("index must be >= 0; got: %s" % index_static)
    if (index_static is not None and num_shards_static is not None and
        index_static >= num_shards_static):
      raise ValueError("index must be <= num_shards; %s is not < %s" %
                       (index_static, num_shards_static))

    def filter_fn(elem_index, _):
      mod_result = math_ops.mod(elem_index, num_shards)
      return math_ops.equal(mod_result, index)

    return self._enumerate().filter(filter_fn).map(lambda _, elem: elem)

  def batch(self, batch_size):
    """Combines consecutive elements of this dataset into batches.

    Args:
      batch_size: A `tf.int64` scalar `tf.Tensor`, representing the number of
        consecutive elements of this dataset to combine in a single batch.

    Returns:
      A `Dataset`.
    """
    return BatchDataset(self, batch_size)

  def padded_batch(self, batch_size, padded_shapes, padding_values=None):
    """Combines consecutive elements of this dataset into padded batches.

    Like `Dataset.dense_to_sparse_batch()`, this method combines
    multiple consecutive elements of this dataset, which might have
    different shapes, into a single element. The tensors in the
    resulting element have an additional outer dimension, and are
    padded to the respective shape in `padded_shapes`.

    Args:
      batch_size: A `tf.int64` scalar `tf.Tensor`, representing the number of
        consecutive elements of this dataset to combine in a single batch.
      padded_shapes: A nested structure of `tf.TensorShape` or
        `tf.int64` vector tensor-like objects representing the shape
        to which the respective component of each input element should
        be padded prior to batching. Any unknown dimensions
        (e.g. `tf.Dimension(None)` in a `tf.TensorShape` or `-1` in a
        tensor-like object) will be padded to the maximum size of that
        dimension in each batch.
      padding_values: (Optional.) A nested structure of scalar-shaped
        `tf.Tensor`, representing the padding values to use for the
        respective components.  Defaults are `0` for numeric types and
        the empty string for string types.

    Returns:
      A `Dataset`.
    """
    return PaddedBatchDataset(self, batch_size, padded_shapes, padding_values)

  def map(self,
          map_func,
          num_threads=None,
          output_buffer_size=None,
          num_parallel_calls=None):
    """Maps `map_func` across this datset.

    Args:
      map_func: A function mapping a nested structure of tensors (having
        shapes and types defined by `self.output_shapes` and
       `self.output_types`) to another nested structure of tensors.
      num_threads: (Optional.) Deprecated, use `num_parallel_calls` instead.
      output_buffer_size: (Optional.) A `tf.int64` scalar `tf.Tensor`,
        representing the maximum number of processed elements that will be
        buffered.
      num_parallel_calls: (Optional.) A `tf.int32` scalar `tf.Tensor`,
        representing the number elements to process in parallel. If not
        specified, elements will be processed sequentially.

    Returns:
      A `Dataset`.
    """
    if num_threads is None and num_parallel_calls is None:
      ret = MapDataset(self, map_func)
    else:
      if num_threads is None:
        ret = ParallelMapDataset(self, map_func, num_parallel_calls)
      else:
        ret = ParallelMapDataset(self, map_func, num_threads)
    if output_buffer_size is not None:
      ret = ret.prefetch(output_buffer_size)
    return ret

  def flat_map(self, map_func):
    """Maps `map_func` across this dataset and flattens the result.

    Args:
      map_func: A function mapping a nested structure of tensors (having shapes
        and types defined by `self.output_shapes` and `self.output_types`) to a
        `Dataset`.

    Returns:
      A `Dataset`.
    """
    return FlatMapDataset(self, map_func)

  def interleave(self, map_func, cycle_length, block_length=1):
    """Maps `map_func` across this dataset, and interleaves the results.

    For example, you can use `Dataset.interleave()` to process many input files
    concurrently:

    ```python
    # Preprocess 4 files concurrently, and interleave blocks of 16 records from
    # each file.
    filenames = ["/var/data/file1.txt", "/var/data/file2.txt", ..."]
    dataset = (Dataset.from_tensor_slices(filenames)
               .interleave(lambda x:
                   TextLineDataset(x).map(parse_fn, num_parallel_calls=1),
                   cycle_length=4, block_length=16))
    ```

    The `cycle_length` and `block_length` arguments control the order in which
    elements are produced. `cycle_length` controls the number of input elements
    that are processed concurrently. If you set `cycle_length` to 1, this
    transformation will handle one input element at a time, and will produce
    identical results = to @{tf.data.Dataset.flat_map}. In general,
    this transformation will apply `map_func` to `cycle_length` input elements,
    open iterators on the returned `Dataset` objects, and cycle through them
    producing `block_length` consecutive elements from each iterator, and
    consuming the next input element each time it reaches the end of an
    iterator.

    For example:

    ```python
    # NOTE: The following examples use `{ ... }` to represent the
    # contents of a dataset.
    a = { 1, 2, 3, 4, 5 }

    # NOTE: New lines indicate "block" boundaries.
    a.interleave(lambda x: Dataset.from_tensors(x).repeat(6),
                 cycle_length=2, block_length=4) == {
        1, 1, 1, 1,
        2, 2, 2, 2,
        1, 1,
        2, 2,
        3, 3, 3, 3,
        4, 4, 4, 4,
        3, 3,
        4, 4,
        5, 5, 5, 5,
        5, 5,
    }
    ```

    NOTE: The order of elements yielded by this transformation is
    deterministic, as long as `map_func` is a pure function. If
    `map_func` contains any stateful operations, the order in which
    that state is accessed is undefined.

    Args:
      map_func: A function mapping a nested structure of tensors (having shapes
        and types defined by `self.output_shapes` and `self.output_types`) to a
        `Dataset`.
      cycle_length: The number of elements from this dataset that will be
        processed concurrently.
      block_length: The number of consecutive elements to produce from each
        input element before cycling to another input element.

    Returns:
      A `Dataset`.
    """
    return InterleaveDataset(self, map_func, cycle_length, block_length)

  def filter(self, predicate):
    """Filters this dataset according to `predicate`.

    Args:
      predicate: A function mapping a nested structure of tensors (having shapes
        and types defined by `self.output_shapes` and `self.output_types`) to a
        scalar `tf.bool` tensor.

    Returns:
      A `Dataset`.
    """
    return FilterDataset(self, predicate)

  def apply(self, transformation_func):
    """Apply a transformation function to this dataset.

    `apply` enables chaining of custom `Dataset` transformations, which are
    represented as functions that take one `Dataset` argument and return a
    transformed `Dataset`.

    For example:

    ```
    dataset = (dataset.map(lambda x: x ** 2)
               .apply(group_by_window(key_func, reduce_func, window_size))
               .map(lambda x: x ** 3))
    ```

    Args:
      transformation_func: A function that takes one `Dataset` argument and
        returns a `Dataset`.

    Returns:
      The `Dataset` returned by applying `transformation_func` to this dataset.
    """
    dataset = transformation_func(self)
    if not isinstance(dataset, Dataset):
      raise TypeError("`transformation_func` must return a Dataset.")
    return dataset


class TensorDataset(Dataset):
  """A `Dataset` with a single element, viz. a nested structure of tensors."""

  def __init__(self, tensors):
    """See `Dataset.from_tensors()` for details."""
    super(TensorDataset, self).__init__()
    with ops.name_scope("tensors"):
      self._tensors = nest.pack_sequence_as(tensors, [
          ops.convert_to_tensor(t, name="component_%d" % i)
          for i, t in enumerate(nest.flatten(tensors))
      ])

  def make_dataset_resource(self):
    return gen_dataset_ops.tensor_dataset(
        nest.flatten(self._tensors),
        output_shapes=nest.flatten(self.output_shapes))

  @property
  def output_shapes(self):
    return nest.pack_sequence_as(self._tensors,
                                 [t.shape for t in nest.flatten(self._tensors)])

  @property
  def output_types(self):
    return nest.pack_sequence_as(self._tensors,
                                 [t.dtype for t in nest.flatten(self._tensors)])


class TensorSliceDataset(Dataset):
  """A `Dataset` of slices from a nested structure of tensors."""

  def __init__(self, tensors):
    """See `Dataset.from_tensor_slices()` for details."""
    super(TensorSliceDataset, self).__init__()
    with ops.name_scope("tensors"):
      flat_tensors = [
          ops.convert_to_tensor(t, name="component_%d" % i)
          for i, t in enumerate(nest.flatten(tensors))
      ]

    self._tensors = nest.pack_sequence_as(tensors, flat_tensors)
    batch_dim = flat_tensors[0].get_shape()[0]
    for t in flat_tensors[1:]:
      batch_dim.assert_is_compatible_with(t.get_shape()[0])

  def make_dataset_resource(self):
    return gen_dataset_ops.tensor_slice_dataset(
        nest.flatten(self._tensors),
        output_shapes=nest.flatten(self.output_shapes))

  @property
  def output_shapes(self):
    return nest.pack_sequence_as(self._tensors, [
        tensor_shape.TensorShape(t.shape[1:])
        for t in nest.flatten(self._tensors)
    ])

  @property
  def output_types(self):
    return nest.pack_sequence_as(self._tensors,
                                 [t.dtype for t in nest.flatten(self._tensors)])


class SparseTensorSliceDataset(Dataset):
  """A `Dataset` that splits a rank-N `tf.SparseTensor` into its rows."""

  def __init__(self, sparse_tensor):
    """See `Dataset.from_sparse_tensor_slices()` for details."""
    super(SparseTensorSliceDataset, self).__init__()
    if not isinstance(sparse_tensor, sparse_tensor_lib.SparseTensor):
      raise TypeError("`sparse_tensor` must be a `tf.SparseTensor` object.")
    self._sparse_tensor = sparse_tensor

  def make_dataset_resource(self):
    return gen_dataset_ops.sparse_tensor_slice_dataset(
        self._sparse_tensor.indices, self._sparse_tensor.values,
        self._sparse_tensor.dense_shape)

  @property
  def output_shapes(self):
    indices_shape = self._sparse_tensor.indices.get_shape()
    shape_shape = self._sparse_tensor.dense_shape.get_shape()
    rank = (indices_shape[1] - 1).merge_with(shape_shape[0] - 1)
    num_values = tensor_shape.Dimension(None)
    return (tensor_shape.TensorShape([num_values, rank]),
            tensor_shape.TensorShape([num_values]), tensor_shape.TensorShape(
                [rank]))

  @property
  def output_types(self):
    return (dtypes.int64, self._sparse_tensor.dtype, dtypes.int64)


class ZipDataset(Dataset):
  """A `Dataset` that zips its inputs together."""

  def __init__(self, datasets):
    """See `Dataset.zip()` for details."""
    super(ZipDataset, self).__init__()
    self._datasets = datasets

  def make_dataset_resource(self):
    return gen_dataset_ops.zip_dataset(
        [ds.make_dataset_resource() for ds in nest.flatten(self._datasets)],
        output_shapes=[
            s
            for ds in nest.flatten(self._datasets)
            for s in nest.flatten(ds.output_shapes)
        ],
        output_types=[
            t
            for ds in nest.flatten(self._datasets)
            for t in nest.flatten(ds.output_types)
        ])

  @property
  def output_shapes(self):
    return nest.pack_sequence_as(self._datasets, [
        ds.output_shapes for ds in nest.flatten(self._datasets)
    ])

  @property
  def output_types(self):
    return nest.pack_sequence_as(self._datasets, [
        ds.output_types for ds in nest.flatten(self._datasets)
    ])


class ConcatenateDataset(Dataset):
  """A `Dataset` that concatenates its input with given dataset."""

  def __init__(self, input_dataset, dataset_to_concatenate):
    """See `Dataset.concatenate()` for details."""
    super(ConcatenateDataset, self).__init__()
    self._input_dataset = input_dataset
    self._dataset_to_concatenate = dataset_to_concatenate
    nest.assert_same_structure(input_dataset.output_types,
                               dataset_to_concatenate.output_types)
    for a, b in zip(
        nest.flatten(input_dataset.output_types),
        nest.flatten(dataset_to_concatenate.output_types)):
      if a != b:
        raise TypeError(
            "Two datasets to concatenate have different types %s and %s" %
            (input_dataset.output_types, dataset_to_concatenate.output_types))

  def make_dataset_resource(self):
    return gen_dataset_ops.concatenate_dataset(
        self._input_dataset.make_dataset_resource(),
        self._dataset_to_concatenate.make_dataset_resource(),
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    return nest.pack_sequence_as(self._input_dataset.output_shapes, [
        ts1.most_specific_compatible_shape(ts2)
        for (ts1, ts2) in zip(
            nest.flatten(self._input_dataset.output_shapes),
            nest.flatten(self._dataset_to_concatenate.output_shapes))
    ])

  @property
  def output_types(self):
    return self._input_dataset.output_types


class RepeatDataset(Dataset):
  """A `Dataset` that repeats its input several times."""

  def __init__(self, input_dataset, count):
    """See `Dataset.repeat()` for details."""
    super(RepeatDataset, self).__init__()
    self._input_dataset = input_dataset
    if count is None:
      self._count = constant_op.constant(-1, dtype=dtypes.int64, name="count")
    else:
      self._count = ops.convert_to_tensor(
          count, dtype=dtypes.int64, name="count")

  def make_dataset_resource(self):
    return gen_dataset_ops.repeat_dataset(
        self._input_dataset.make_dataset_resource(),
        count=self._count,
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    return self._input_dataset.output_shapes

  @property
  def output_types(self):
    return self._input_dataset.output_types


class RangeDataset(Dataset):
  """A `Dataset` of a step separated range of values."""

  def __init__(self, *args):
    """See `Dataset.range()` for details."""
    super(RangeDataset, self).__init__()
    self._parse_args(*args)

  def _parse_args(self, *args):
    if len(args) == 1:
      self._start = self._build_tensor(0, "start")
      self._stop = args[0]
      self._step = self._build_tensor(1, "step")
    elif len(args) == 2:
      self._start = args[0]
      self._stop = args[1]
      self._step = self._build_tensor(1, "step")
    elif len(args) == 3:
      self._start = args[0]
      self._stop = args[1]
      self._step = args[2]
    else:
      raise ValueError("Invalid arguments to RangeDataset: %s" % str(args))

  def _build_tensor(self, int64_value, name):
    return constant_op.constant(int64_value, dtype=dtypes.int64, name=name)

  def make_dataset_resource(self):
    return gen_dataset_ops.range_dataset(
        start=self._start,
        stop=self._stop,
        step=self._step,
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    return tensor_shape.scalar()

  @property
  def output_types(self):
    return dtypes.int64


class CacheDataset(Dataset):
  """A `Dataset` that caches elements of its input."""

  def __init__(self, input_dataset, filename):
    """See `Dataset.cache()` for details."""
    super(CacheDataset, self).__init__()
    self._input_dataset = input_dataset
    self._filename = ops.convert_to_tensor(
        filename, dtype=dtypes.string, name="filename")

  def make_dataset_resource(self):
    return gen_dataset_ops.cache_dataset(
        self._input_dataset.make_dataset_resource(),
        filename=self._filename,
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    return self._input_dataset.output_shapes

  @property
  def output_types(self):
    return self._input_dataset.output_types


class ShuffleDataset(Dataset):
  """A `Dataset` that randomly shuffles the elements of its input."""

  def __init__(self, input_dataset, buffer_size, seed=None):
    """See `Dataset.shuffle()` for details."""
    super(ShuffleDataset, self).__init__()
    self._input_dataset = input_dataset
    self._buffer_size = ops.convert_to_tensor(
        buffer_size, dtype=dtypes.int64, name="buffer_size")
    seed, seed2 = random_seed.get_seed(seed)
    if seed is None:
      self._seed = constant_op.constant(0, dtype=dtypes.int64, name="seed")
    else:
      self._seed = ops.convert_to_tensor(seed, dtype=dtypes.int64, name="seed")
    if seed2 is None:
      self._seed2 = constant_op.constant(0, dtype=dtypes.int64, name="seed2")
    else:
      self._seed2 = ops.convert_to_tensor(
          seed2, dtype=dtypes.int64, name="seed2")

  def make_dataset_resource(self):
    return gen_dataset_ops.shuffle_dataset(
        self._input_dataset.make_dataset_resource(),
        buffer_size=self._buffer_size,
        seed=self._seed,
        seed2=self._seed2,
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    return self._input_dataset.output_shapes

  @property
  def output_types(self):
    return self._input_dataset.output_types


class TakeDataset(Dataset):
  """A `Dataset` containing the first `count` elements from its input."""

  def __init__(self, input_dataset, count):
    """See `Dataset.take()` for details."""
    super(TakeDataset, self).__init__()
    self._input_dataset = input_dataset
    self._count = ops.convert_to_tensor(count, dtype=dtypes.int64, name="count")

  def make_dataset_resource(self):
    return gen_dataset_ops.take_dataset(
        self._input_dataset.make_dataset_resource(),
        count=self._count,
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    return self._input_dataset.output_shapes

  @property
  def output_types(self):
    return self._input_dataset.output_types


class SkipDataset(Dataset):
  """A `Dataset` skipping the first `count` elements from its input."""

  def __init__(self, input_dataset, count):
    """See `Dataset.skip()` for details."""
    super(SkipDataset, self).__init__()
    self._input_dataset = input_dataset
    self._count = ops.convert_to_tensor(count, dtype=dtypes.int64, name="count")

  def make_dataset_resource(self):
    return gen_dataset_ops.skip_dataset(
        self._input_dataset.make_dataset_resource(),
        count=self._count,
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    return self._input_dataset.output_shapes

  @property
  def output_types(self):
    return self._input_dataset.output_types


class BatchDataset(Dataset):
  """A `Dataset` that batches contiguous elements from its input."""

  def __init__(self, input_dataset, batch_size):
    """See `Dataset.batch()` for details."""
    super(BatchDataset, self).__init__()
    self._input_dataset = input_dataset
    self._batch_size = batch_size

  def make_dataset_resource(self):
    return gen_dataset_ops.batch_dataset(
        self._input_dataset.make_dataset_resource(),
        batch_size=self._batch_size,
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    input_shapes = self._input_dataset.output_shapes
    return nest.pack_sequence_as(input_shapes, [
        tensor_shape.vector(None).concatenate(s)
        for s in nest.flatten(self._input_dataset.output_shapes)
    ])

  @property
  def output_types(self):
    return self._input_dataset.output_types


def _partial_shape_to_tensor(shape_like):
  try:
    # First attempt to convert the input to a shape, and return the
    # "canonical" tensor representation, which uses `-1` in place of
    # `None`.
    shape_like = tensor_shape.as_shape(shape_like)
    return ops.convert_to_tensor(
        [dim if dim is not None else -1 for dim in shape_like.as_list()],
        dtype=dtypes.int64)
  except (TypeError, ValueError):
    # The argument was not trivially convertible to a
    # `tf.TensorShape`, so fall back on the conversion to tensor
    # machinery.
    return ops.convert_to_tensor(shape_like, dtype=dtypes.int64)


def _padding_value_to_tensor(value, output_type):
  """Converts the padding value to a tensor.

  Args:
    value: The padding value.
    output_type: Its expected dtype.

  Returns:
    A scalar `Tensor`.

  Raises:
    ValueError: if the padding value is not a scalar.
    TypeError: if the padding value's type does not match `output_type`.
  """
  value = ops.convert_to_tensor(value, name="padding_value")
  if not value.shape.is_compatible_with(tensor_shape.scalar()):
    raise ValueError("Padding value should be a scalar, but is not: %s" % value)
  if value.dtype != output_type:
    raise TypeError("Padding value tensor (%s) does not match output type: %s" %
                    (value, output_type))
  return value


class PaddedBatchDataset(Dataset):
  """A `Dataset` that batches and pads contiguous elements from its input."""

  def __init__(self, input_dataset, batch_size, padded_shapes, padding_values):
    """See `Dataset.batch()` for details."""
    super(PaddedBatchDataset, self).__init__()
    self._input_dataset = input_dataset
    self._batch_size = batch_size
    padding_values = (padding_values if padding_values is not None else
                      self._default_padding(input_dataset))
    self._padded_shapes = nest.map_structure_up_to(
        input_dataset.output_shapes, _partial_shape_to_tensor, padded_shapes)
    self._padding_values = nest.map_structure_up_to(
        input_dataset.output_shapes, _padding_value_to_tensor, padding_values,
        input_dataset.output_types)

  def _default_padding(self, input_dataset):

    def make_zero(t):
      if t.base_dtype == dtypes.string:
        return ""
      else:
        return np.zeros_like(t.as_numpy_dtype())

    return nest.map_structure(make_zero, input_dataset.output_types)

  def make_dataset_resource(self):
    return gen_dataset_ops.padded_batch_dataset(
        self._input_dataset.make_dataset_resource(),
        batch_size=self._batch_size,
        padded_shapes=[
            ops.convert_to_tensor(s, dtype=dtypes.int64)
            for s in nest.flatten(self._padded_shapes)
        ],
        padding_values=nest.flatten(self._padding_values),
        output_shapes=nest.flatten(self.output_shapes))

  @property
  def output_shapes(self):

    def _padded_shape_to_batch_shape(s):
      return tensor_shape.vector(None).concatenate(
          tensor_util.constant_value_as_shape(s))

    return nest.map_structure(_padded_shape_to_batch_shape, self._padded_shapes)

  @property
  def output_types(self):
    return self._input_dataset.output_types


def _should_unpack_args(args):
  """Returns `True` if `args` should be `*args` when passed to a callable."""
  return type(args) is tuple  # pylint: disable=unidiomatic-typecheck


class MapDataset(Dataset):
  """A `Dataset` that maps a function over elements in its input."""

  def __init__(self, input_dataset, map_func):
    """See `Dataset.map()` for details."""
    super(MapDataset, self).__init__()
    self._input_dataset = input_dataset

    self._output_shapes = None
    self._output_types = None

    @function.Defun(*nest.flatten(input_dataset.output_types))
    def tf_map_func(*args):
      """A wrapper for Defun that facilitates shape inference."""
      # Pass in shape information from the input_dataset.
      for arg, shape in zip(args, nest.flatten(input_dataset.output_shapes)):
        arg.set_shape(shape)

      nested_args = nest.pack_sequence_as(input_dataset.output_types, args)

      if _should_unpack_args(nested_args):
        ret = map_func(*nested_args)
      else:
        ret = map_func(nested_args)

      # If `map_func` returns a list of tensors, `nest.flatten()` and
      # `ops.convert_to_tensor()` would conspire to attempt to stack
      # those tensors into a single tensor, because the customized
      # version of `nest.flatten()` does not recurse into lists. Since
      # it is more likely that the list arose from returning the
      # result of an operation (such as `tf.py_func()`) that returns a
      # list of not-necessarily-stackable tensors, we treat the
      # returned value is a `tuple` instead. A user wishing to pack
      # the return value into a single tensor can use an explicit
      # `tf.stack()` before returning.
      if isinstance(ret, list):
        ret = tuple(ret)

      # Extract shape information from the returned values.
      flattened_ret = [ops.convert_to_tensor(t) for t in nest.flatten(ret)]
      self._output_shapes = nest.pack_sequence_as(
          ret, [t.get_shape() for t in flattened_ret])
      self._output_types = nest.pack_sequence_as(
          ret, [t.dtype for t in flattened_ret])

      return flattened_ret

    self._map_func = tf_map_func
    self._map_func.add_to_graph(ops.get_default_graph())

  def make_dataset_resource(self):
    input_resource = self._input_dataset.make_dataset_resource()
    return gen_dataset_ops.map_dataset(
        input_resource,
        self._map_func.captured_inputs,
        f=self._map_func,
        output_types=nest.flatten(self.output_types),
        output_shapes=nest.flatten(self.output_shapes))

  @property
  def output_shapes(self):
    return self._output_shapes

  @property
  def output_types(self):
    return self._output_types


class ParallelMapDataset(MapDataset):
  """A `Dataset` that maps a function over elements in its input in parallel."""

  def __init__(self, input_dataset, map_func, num_parallel_calls):
    """See `Dataset.map()` for details."""
    super(ParallelMapDataset, self).__init__(input_dataset, map_func)

    self._num_parallel_calls = ops.convert_to_tensor(
        num_parallel_calls, dtype=dtypes.int32, name="num_parallel_calls")

  def make_dataset_resource(self):
    input_resource = self._input_dataset.make_dataset_resource()
    # pylint: disable=protected-access
    return gen_dataset_ops.parallel_map_dataset(
        input_resource,
        self._map_func.captured_inputs,
        f=self._map_func,
        num_parallel_calls=self._num_parallel_calls,
        output_types=nest.flatten(self.output_types),
        output_shapes=nest.flatten(self.output_shapes))
    # pylint: enable=protected-access


class FlatMapDataset(Dataset):
  """A `Dataset` that maps a function over its input and flattens the result."""

  def __init__(self, input_dataset, map_func):
    """See `Dataset.flat_map()` for details."""
    super(FlatMapDataset, self).__init__()
    self._input_dataset = input_dataset

    @function.Defun(*nest.flatten(input_dataset.output_types))
    def tf_map_func(*args):
      """A wrapper for Defun that facilitates shape inference."""
      # Pass in shape information from the input_dataset.
      for arg, shape in zip(args, nest.flatten(input_dataset.output_shapes)):
        arg.set_shape(shape)

      nested_args = nest.pack_sequence_as(input_dataset.output_types, args)

      if _should_unpack_args(nested_args):
        dataset = map_func(*nested_args)
      else:
        dataset = map_func(nested_args)

      if not isinstance(dataset, Dataset):
        raise TypeError("`map_func` must return a `Dataset` object.")

      self._output_types = dataset.output_types
      self._output_shapes = dataset.output_shapes

      return dataset.make_dataset_resource()

    self._map_func = tf_map_func
    self._map_func.add_to_graph(ops.get_default_graph())

  def make_dataset_resource(self):
    return gen_dataset_ops.flat_map_dataset(
        self._input_dataset.make_dataset_resource(),
        self._map_func.captured_inputs,
        f=self._map_func,
        output_types=nest.flatten(self.output_types),
        output_shapes=nest.flatten(self.output_shapes))

  @property
  def output_shapes(self):
    return self._output_shapes

  @property
  def output_types(self):
    return self._output_types


class InterleaveDataset(Dataset):
  """A `Dataset` that maps a function over its input and interleaves the result.
  """

  def __init__(self, input_dataset, map_func, cycle_length, block_length):
    """See `Dataset.interleave()` for details."""
    super(InterleaveDataset, self).__init__()
    self._input_dataset = input_dataset

    @function.Defun(*nest.flatten(input_dataset.output_types))
    def tf_map_func(*args):
      """A wrapper for Defun that facilitates shape inference."""
      # Pass in shape information from the input_dataset.
      for arg, shape in zip(args, nest.flatten(input_dataset.output_shapes)):
        arg.set_shape(shape)

      nested_args = nest.pack_sequence_as(input_dataset.output_types, args)

      if _should_unpack_args(nested_args):
        dataset = map_func(*nested_args)
      else:
        dataset = map_func(nested_args)

      if not isinstance(dataset, Dataset):
        raise TypeError("`map_func` must return a `Dataset` object.")

      self._output_types = dataset.output_types
      self._output_shapes = dataset.output_shapes

      return dataset.make_dataset_resource()

    self._map_func = tf_map_func
    self._map_func.add_to_graph(ops.get_default_graph())

    self._cycle_length = ops.convert_to_tensor(cycle_length, dtype=dtypes.int64)
    self._block_length = ops.convert_to_tensor(block_length, dtype=dtypes.int64)

  def make_dataset_resource(self):
    return gen_dataset_ops.interleave_dataset(
        self._input_dataset.make_dataset_resource(),
        self._map_func.captured_inputs,
        self._cycle_length,
        self._block_length,
        f=self._map_func,
        output_types=nest.flatten(self.output_types),
        output_shapes=nest.flatten(self.output_shapes))

  @property
  def output_shapes(self):
    return self._output_shapes

  @property
  def output_types(self):
    return self._output_types


class FilterDataset(Dataset):
  """A `Dataset` that filters its input according to a predicate function."""

  def __init__(self, input_dataset, predicate):
    """See `Dataset.filter()` for details."""
    super(FilterDataset, self).__init__()
    self._input_dataset = input_dataset

    @function.Defun(*nest.flatten(input_dataset.output_types))
    def tf_predicate(*args):
      """A wrapper for Defun that facilitates shape inference."""
      # Pass in shape information from the input_dataset.
      for arg, shape in zip(args, nest.flatten(input_dataset.output_shapes)):
        arg.set_shape(shape)

      nested_args = nest.pack_sequence_as(input_dataset.output_types, args)

      if _should_unpack_args(nested_args):
        ret = predicate(*nested_args)
      else:
        ret = predicate(nested_args)

      ret = ops.convert_to_tensor(ret, dtype=dtypes.bool)
      if not (ret.dtype == dtypes.bool and
              ret.shape.is_compatible_with(tensor_shape.scalar())):
        raise ValueError("`predicate` must return a scalar boolean tensor.")

      return ret

    self._predicate = tf_predicate
    self._predicate.add_to_graph(ops.get_default_graph())

  def make_dataset_resource(self):
    return gen_dataset_ops.filter_dataset(
        self._input_dataset.make_dataset_resource(),
        other_arguments=self._predicate.captured_inputs,
        predicate=self._predicate,
        output_types=nest.flatten(self.output_types),
        output_shapes=nest.flatten(self.output_shapes))

  @property
  def output_shapes(self):
    return self._input_dataset.output_shapes

  @property
  def output_types(self):
    return self._input_dataset.output_types


class PrefetchDataset(Dataset):
  """A `Dataset` that asynchronously prefetches its input."""

  def __init__(self, input_dataset, buffer_size):
    """See `Dataset.prefetch()` for details."""
    super(PrefetchDataset, self).__init__()
    self._input_dataset = input_dataset
    self._buffer_size = ops.convert_to_tensor(buffer_size, dtype=dtypes.int64)

  def make_dataset_resource(self):
    return gen_dataset_ops.prefetch_dataset(
        self._input_dataset.make_dataset_resource(),
        buffer_size=self._buffer_size,
        output_shapes=nest.flatten(self.output_shapes),
        output_types=nest.flatten(self.output_types))

  @property
  def output_shapes(self):
    return self._input_dataset.output_shapes

  @property
  def output_types(self):
    return self._input_dataset.output_types


# TODO(b/64974358): Increase default buffer size to 256 MB.
_DEFAULT_READER_BUFFER_SIZE_BYTES = 256 * 1024  # 256 KB


def _convert_optional_param_to_tensor(argument_name,
                                      argument_value,
                                      argument_default=0,
                                      argument_dtype=dtypes.int64):
  if argument_value is not None:
    return ops.convert_to_tensor(
        argument_value, dtype=argument_dtype, name=argument_name)
  else:
    return constant_op.constant(
        argument_default, dtype=argument_dtype, name=argument_name)


class TextLineDataset(Dataset):
  """A `Dataset` comprising lines from one or more text files."""

  def __init__(self, filenames, compression_type=None, buffer_size=None):
    """Creates a `TextLineDataset`.

    Args:
      filenames: A `tf.string` tensor containing one or more filenames.
      compression_type: (Optional.) A `tf.string` scalar evaluating to one of
        `""` (no compression), `"ZLIB"`, or `"GZIP"`.
      buffer_size: (Optional.) A `tf.int64` scalar denoting the number of bytes
        to buffer. A value of 0 results in the default buffering values chosen
        based on the compression type.
    """
    super(TextLineDataset, self).__init__()
    self._filenames = ops.convert_to_tensor(
        filenames, dtype=dtypes.string, name="filenames")
    self._compression_type = _convert_optional_param_to_tensor(
        "compression_type",
        compression_type,
        argument_default="",
        argument_dtype=dtypes.string)
    self._buffer_size = _convert_optional_param_to_tensor(
        "buffer_size", buffer_size, _DEFAULT_READER_BUFFER_SIZE_BYTES)

  def make_dataset_resource(self):
    return gen_dataset_ops.text_line_dataset(
        self._filenames, self._compression_type, self._buffer_size)

  @property
  def output_shapes(self):
    return tensor_shape.scalar()

  @property
  def output_types(self):
    return dtypes.string


class TFRecordDataset(Dataset):
  """A `Dataset` comprising records from one or more TFRecord files."""

  def __init__(self, filenames, compression_type=None, buffer_size=None):
    """Creates a `TFRecordDataset`.

    Args:
      filenames: A `tf.string` tensor containing one or more filenames.
      compression_type: (Optional.) A `tf.string` scalar evaluating to one of
        `""` (no compression), `"ZLIB"`, or `"GZIP"`.
      buffer_size: (Optional.) A `tf.int64` scalar representing the number of
        bytes in the read buffer. 0 means no buffering.
    """
    super(TFRecordDataset, self).__init__()
    # Force the type to string even if filenames is an empty list.
    self._filenames = ops.convert_to_tensor(
        filenames, dtypes.string, name="filenames")
    self._compression_type = _convert_optional_param_to_tensor(
        "compression_type",
        compression_type,
        argument_default="",
        argument_dtype=dtypes.string)
    self._buffer_size = _convert_optional_param_to_tensor(
        "buffer_size",
        buffer_size,
        argument_default=_DEFAULT_READER_BUFFER_SIZE_BYTES)

  def make_dataset_resource(self):
    return gen_dataset_ops.tf_record_dataset(
        self._filenames, self._compression_type, self._buffer_size)

  @property
  def output_shapes(self):
    return tensor_shape.TensorShape([])

  @property
  def output_types(self):
    return dtypes.string


class FixedLengthRecordDataset(Dataset):
  """A `Dataset` of fixed-length records from one or more binary files."""

  def __init__(self,
               filenames,
               record_bytes,
               header_bytes=None,
               footer_bytes=None,
               buffer_size=None):
    """Creates a `FixedLengthRecordDataset`.

    Args:
      filenames: A `tf.string` tensor containing one or more filenames.
      record_bytes: A `tf.int64` scalar representing the number of bytes in
        each record.
      header_bytes: (Optional.) A `tf.int64` scalar representing the number of
        bytes to skip at the start of a file.
      footer_bytes: (Optional.) A `tf.int64` scalar representing the number of
        bytes to ignore at the end of a file.
      buffer_size: (Optional.) A `tf.int64` scalar representing the number of
        bytes to buffer when reading.
    """
    super(FixedLengthRecordDataset, self).__init__()
    self._filenames = ops.convert_to_tensor(
        filenames, dtype=dtypes.string, name="filenames")
    self._record_bytes = ops.convert_to_tensor(
        record_bytes, dtype=dtypes.int64, name="record_bytes")

    self._header_bytes = _convert_optional_param_to_tensor(
        "header_bytes", header_bytes)
    self._footer_bytes = _convert_optional_param_to_tensor(
        "footer_bytes", footer_bytes)
    self._buffer_size = _convert_optional_param_to_tensor(
        "buffer_size", buffer_size, _DEFAULT_READER_BUFFER_SIZE_BYTES)

  def make_dataset_resource(self):
    return gen_dataset_ops.fixed_length_record_dataset(
        self._filenames, self._header_bytes, self._record_bytes,
        self._footer_bytes, self._buffer_size)

  @property
  def output_shapes(self):
    return tensor_shape.scalar()

  @property
  def output_types(self):
    return dtypes.string
