#!/usr/bin/python3
# coding=utf-8

#   Copyright 2024 getcarrier.io
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""
    Event node
"""

from .base import EventNodeBase


class MockEventNode(EventNodeBase):  # pylint: disable=R0902
    """ Event node (local-only mock) - allows to subscribe to events and to emit new events """

    def emit_data(self, data):
        """ Emit event data """
        self.sync_queue.put(data)

    def listening_worker(self):
        """ Listening thread: push event data to sync_queue """
        self.ready_event.set()