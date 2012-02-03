#!/usr/bin/env python

# Software License Agreement (BSD License)
#
# Copyright (c) 2010, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#

NAME="master_sync.py"

import roslib; roslib.load_manifest('rosproxy')

import os
import sys

import roslib.network
import rosgraph.masterapi
import rospy




import os
import sys
import socket
import threading
import traceback
import time
import urlparse

from roslib.xmlrpc import XmlRpcHandler

import rospy.names

import rospy.impl.tcpros

from rospy.core import global_name, is_topic
from rospy.impl.registration import get_topic_manager
from rospy.impl.validators import non_empty, ParameterInvalid

from rospy.impl.masterslave import apivalidate

from roslib.xmlrpc import XmlRpcNode
import roslib.names


def is_publishers_list(paramName):
    return ('is_publishers_list', paramName)


class TopicPubListenerHandler(XmlRpcHandler):

    def __init__(self, cb):
        super(TopicPubListenerHandler, self).__init__()
        self.uri = None
        self.cb = cb

    def _ready(self, uri):
        self.uri = uri

    def _custom_validate(self, validation, param_name, param_value, caller_id):
        if validation == 'is_publishers_list':
            if not type(param_value) == list:
                raise ParameterInvalid("ERROR: param [%s] must be a list"%param_name)
            for v in param_value:
                if not isinstance(v, basestring):
                    raise ParameterInvalid("ERROR: param [%s] must be a list of strings"%param_name)
                parsed = urlparse.urlparse(v)
                if not parsed[0] or not parsed[1]: #protocol and host
                    raise ParameterInvalid("ERROR: param [%s] does not contain valid URLs [%s]"%(param_name, v))
            return param_value
        else:
            raise ParameterInvalid("ERROR: param [%s] has an unknown validation type [%s]"%(param_name, validation))

    @apivalidate([])
    def getBusStats(self, caller_id):
        # not supported
        return 1, '', [[], [], []]

    @apivalidate([])
    def getBusInfo(self, caller_id):
        # not supported
        return 1, '', [[], [], []]
    
    @apivalidate('')
    def getMasterUri(self, caller_id):
        # not supported
        return 1, '', ''
        
    @apivalidate(0, (None, ))
    def shutdown(self, caller_id, msg=''):
        return -1, "not authorized", 0

    @apivalidate(-1)
    def getPid(self, caller_id):
        return -1, "not authorized", 0

    ###############################################################################
    # PUB/SUB APIS

    @apivalidate([])
    def getSubscriptions(self, caller_id):
        return 1, "subscriptions", [[], []]

    @apivalidate([])
    def getPublications(self, caller_id):
        return 1, "publications", [[], []]
    
    @apivalidate(-1, (global_name('parameter_key'), None))
    def paramUpdate(self, caller_id, parameter_key, parameter_value):
        # not supported
        return -1, 'not authorized', 0

    @apivalidate(-1, (is_topic('topic'), is_publishers_list('publishers')))
    def publisherUpdate(self, caller_id, topic, publishers):
        self.cb(topic, publishers)
    
    @apivalidate([], (is_topic('topic'), non_empty('protocols')))
    def requestTopic(self, caller_id, topic, protocols):
        return 0, "no supported protocol implementations", []


class RemoteManager(object):
    def __init__(self, master_uri, cb):
        self.master_uri = master_uri

        ns = roslib.names.get_ros_namespace()
        anon_name = roslib.names.anonymous_name('master_sync')

        self.master = rosgraph.masterapi.Master(roslib.names.ns_join(ns, anon_name), master_uri=self.master_uri)

        self.cb = cb

        self.type_cache = {}

        self.subs = {}
        self.pubs = {}
        self.srvs = {}

        rpc_handler = TopicPubListenerHandler(self.new_topics)
        self.external_node = XmlRpcNode(rpc_handler=rpc_handler)
        self.external_node.start()

        timeout_t = time.time() + 5.
        while time.time() < timeout_t and self.external_node.uri is None:
            time.sleep(0.01)


    def get_topic_type(self, query_topic):
        query_topic = self.resolve(query_topic)

        if query_topic in self.type_cache:
            return self.type_cache[query_topic]
        else:
            for topic, topic_type in self.master.getTopicTypes():
                self.type_cache[topic] = topic_type
            if query_topic in self.type_cache:
                return self.type_cache[query_topic]
            else:
                return "*"

    def subscribe(self, topic):
        topic = self.resolve(topic)
        publishers = self.master.registerSubscriber(topic, '*', self.external_node.uri)        
        self.subs[(topic, self.external_node.uri)] = self.master
        self.new_topics(topic, publishers)

    def advertise(self, topic, topic_type, uri):
        topic = self.resolve(topic)

        # Prevent double-advertisements
        if (topic, uri) in self.pubs:
            return

        # These registrations need to be anonymous so the master doesn't kill us if there is a duplicate name
        anon_name = roslib.names.anonymous_name('master_sync')
        master = rosgraph.masterapi.Master(anon_name, master_uri=self.master_uri)

        rospy.loginfo("Registering (%s,%s) on master %s"%(topic,uri,master.master_uri))

        master.registerPublisher(topic, topic_type, uri)
        self.pubs[(topic, uri)] = master


    def unadvertise(self, topic, uri):
        if (topic, uri) in self.pubs:
            m = self.pubs[(topic,uri)]
            rospy.loginfo("Unregistering (%s,%s) from master %s"%(topic,uri,m.master_uri))
            m.unregisterPublisher(topic,uri)
            del self.pubs[(topic,uri)]


    def advertise_list(self, topic, topic_type, uris):
        topic = self.resolve(topic)

        unadv = set((t,u) for (t,u) in self.pubs.iterkeys() if t == topic) - set([(topic, u) for u in uris])
        for (t,u) in self.pubs.keys():
            if (t,u) in unadv:
                self.unadvertise(t,u)

        for u in uris:
            self.advertise(topic, topic_type, u)

    def lookup_service(self, service_name):
        service_name = self.resolve(service_name)
        try:
            return self.master.lookupService(service_name)
        except rosgraph.masterapi.Error:
            return None

    def advertise_service(self, service_name, uri):

        # These registrations need to be anonymous so the master doesn't kill us if there is a duplicate name
        anon_name = roslib.names.anonymous_name('master_sync')
        master = rosgraph.masterapi.Master(anon_name, master_uri=self.master_uri)

        if (service_name) in self.srvs:
            if self.srvs[service_name][0] == uri:
                return
            else:
                self.unadvertise_service(service_name)

        fake_api = 'http://%s:0'%roslib.network.get_host_name()
        rospy.loginfo("Registering service (%s,%s) on master %s"%(service_name, uri, master.master_uri))
        master.registerService(service_name, uri, fake_api)

        self.srvs[service_name] = (uri, master)

    def unadvertise_service(self, service_name):
        if service_name in self.srvs:
            uri,m = self.srvs[service_name]
            rospy.loginfo("Unregistering service (%s,%s) from master %s"%(service_name, uri, m.master_uri))
            m.unregisterService(service_name, uri)
            del self.srvs[service_name]


    def resolve(self, topic):
        ns = roslib.names.namespace(self.master.caller_id)
        return roslib.names.ns_join(ns, topic)

    def unsubscribe_all(self):
        for (t,u),m in self.subs.iteritems():
            m.unregisterSubscriber(t,u)
        for t,u in self.pubs.keys():
            self.unadvertise(t,u)
        for s in self.srvs.keys():
            self.unadvertise_service(s)
        
    def new_topics(self, topic, publishers):
        self.cb(topic, [p for p in publishers if (topic,p) not in self.pubs])


def check_master(m):
    try:
        m.getUri()
        return True
    except Exception:
        return False

class MasterSync(object):
    def __init__(self):
        rospy.init_node('master_sync')


        self.local_service_names = rospy.get_param('~local_services', {})
        self.local_pub_names = rospy.get_param('~local_pubs', [])
        self.foreign_service_names = rospy.get_param('~foreign_services', {})
        self.foreign_pub_names = rospy.get_param('~foreign_pubs', [])

        # Get master URIs
        lm = roslib.rosenv.get_master_uri()
        fm = rospy.get_param('~foreign_master', 'http://localhost:11312')

        m = rosgraph.masterapi.Master(rospy.get_name(), master_uri=fm)
        r = rospy.Rate(1)
        while not check_master(m) and not rospy.is_shutdown():
            rospy.loginfo("Waiting for foreign master to come up...")
            r.sleep()

        self.local_manager = None
        self.foreign_manager = None

        if not rospy.is_shutdown():

            self.local_manager = RemoteManager(lm, self.new_local_topics)
            self.foreign_manager = RemoteManager(fm, self.new_foreign_topics)

            for t in self.local_pub_names:
                self.local_manager.subscribe(t)

            for t in self.foreign_pub_names:
                self.foreign_manager.subscribe(t)
        
    def new_local_topics(self, topic, publishers):
        topic_type = self.local_manager.get_topic_type(topic)
        self.foreign_manager.advertise_list(topic, topic_type, publishers)


    def new_foreign_topics(self, topic, publishers):
        topic_type = self.foreign_manager.get_topic_type(topic)
        self.local_manager.advertise_list(topic, topic_type, publishers)


    def spin(self):
        r = rospy.Rate(1.0)
        while not rospy.is_shutdown():
            for s in self.local_service_names:
                srv_uri = self.local_manager.lookup_service(s)
                if srv_uri is not None:
                    self.foreign_manager.advertise_service(s, srv_uri)
                else:
                    self.foreign_manager.unadvertise_service(s)
            for s in self.foreign_service_names:
                srv_uri = self.foreign_manager.lookup_service(s)
                if srv_uri is not None:
                    self.local_manager.advertise_service(s, srv_uri)
                else:
                    self.local_manager.unadvertise_service(s)
            r.sleep()

        if self.local_manager:
            self.local_manager.unsubscribe_all()
        if self.foreign_manager:
            self.foreign_manager.unsubscribe_all()


def master_sync_main():
    r = MasterSync()
    r.spin()
    
if __name__ == '__main__':
    master_sync_main()
