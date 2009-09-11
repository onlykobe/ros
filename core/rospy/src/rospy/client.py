# Software License Agreement (BSD License)
#
# Copyright (c) 2008, Willow Garage, Inc.
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
# Revision $Id$

## rospy client API 

import os
import socket
import struct
import sys
import time
import random
import traceback

# reexport Header message as rospy.Header
from roslib.msg import Header

from rospy.core import *
from rospy.init import *
from rospy.msg import AnyMsg
from rospy.rosout import load_rosout_handlers
from rospy.rostime import get_rostime, get_time, sleep, Time, Duration, init_rostime
from rospy.rosutil import parse_rosrpc_uri

from rospy.service import ServiceException, ServiceDefinition
#use tcp ros implementation of services
from rospy.tcpros_service import Service, ServiceProxy 

import roslib.msg    

## \ingroup clientapi Client API
#  blocks until ROS node is shutdown. Yields activity to other threads.
#  @throws ROSInitException if node is not in a properly initialized state
def spin():
    if not is_initialized():
        raise ROSInitException("client code must call rospy.init_node() first")
    #print "ready"
    try:
        while not is_shutdown():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print "shutting down"
        signal_shutdown('keyboard interrupt')

## \ingroup clientapi
#  @return [str]: copy of sys.argv with ROS remapping arguments removed
def myargv(argv=sys.argv):
    return [a for a in argv if not REMAP in a and not a[0] == '-']


_init_node_args = None

## \ingroup clientapi
## Register client node with the master under the specified name.
## This should be called after Pub/Sub topics have been declared and
## it MUST be called from the main Python thread unless \a
## disable_signals is set to True. Duplicate calls to init_node are
## only allowed if the arguments are identical as the side-effects of
## this method are not reversible.
##
## @param name
## @param argv Command line arguments to this program. ROS reads
## these arguments to find renaming params. Defaults to sys.argv.
## @param anonymous bool: if True, a name will be auto-generated for
##   the node using \a name as the base.  This is useful when you
##   wish to have multiple instances of the same node and don't care
##   about their actual names (e.g. tools, guis). \a name will be
##   used as the stem of the auto-generated name. NOTE: you cannot
##   remap the name of an anonymous node.
## @param log_level int: log level (as defined in roslib.msg.Log).
## @param disable_signals bool: If True, rospy will not register its
##   own signal handlers. You must set this flag if (a) you are unable
##   to call init_node from the main thread and/or you are using rospy
##   in an environment where you need to control your own signal
##   handling (e.g. WX).
## @param disable_rostime bool: for rostests only, suppresses
## automatic subscription to rostime
## @throws ROSInitException if initialization/registration fails
def init_node(name, argv=sys.argv, anonymous=False, log_level=roslib.msg.Log.INFO, disable_rostime=False, disable_signals=False):
    global _init_node_args

    # #972: allow duplicate init_node args if calls are identical
    # NOTE: we don't bother checking for node name aliases (e.g. 'foo' == '/foo').
    if _init_node_args:
        if _init_node_args != (name, argv, anonymous, log_level, disable_rostime, disable_signals):
            raise ROSException("rospy.init_node() has already been called with different arguments: "+str(_init_node_args))
        else:
            return #already initialized
    _init_node_args = (name, argv, anonymous, log_level, disable_rostime, disable_signals)
        
    if not disable_signals:
        # NOTE: register_signals must be called from main thread
        register_signals() # add handlers for SIGINT/etc...
    else:
        logging.getLogger("rospy.client").warn("signal handlers for rospy disabled")

    # check for name override
    name_remap = resolve_name('__name', '/')
    if name_remap != '/__name':
        # re-resolve, using actual namespace
        name = resolve_name('__name')
        if anonymous:
            logger = logging.getLogger("rospy.client")
            logger.info("WARNING: due to __name setting, anonymous setting is being changed to false")
            print >> sys.stderr, "[%s] WARNING: due to __name setting, anonymous setting is being changed to false"%name
            
            anonymous = False
        
    #TODO:fix this temporary hack for anonymous names
    if anonymous:
        name = "%s-%s-%s"%(name, os.getpid(), time.time())

    configure_logging(resolve_name(name))
    
    node = start_node(os.environ, name=name) #node initialization blocks until registration with master
    timeout_t = time.time() + TIMEOUT_READY
    code = None
    while time.time() < timeout_t and code is None:
        try:
            code, msg, master_uri = node.getMasterUri()
        except:
            time.sleep(0.01) #poll for init
    set_initialized(True)
    if code is None:
        raise ROSInitException("ROS node initialization failed: unable to connect to local node")        
    elif code != 1:
        raise ROSInitException("ROS node initialization failed: %s, %s, %s", code, msg, master_uri)

    load_rosout_handlers(log_level)
    if not disable_rostime:
        init_rostime()

## #503
## @deprecated 
ready = init_node

#_master_proxy is a MasterProxy wrapper
_master_proxy = None

## \ingroup clientapi 
# Get a remote handle to the ROS Master. This method can be called
# independent of running a ROS node, though the ROS_MASTER_URI must be
# declared in the environment.
#
# @return MasterProxy: ROS Master remote object
# @throws Exception if server cannot be located or system cannot be
# initalized
def get_master(env=os.environ):
    global _master_proxy
    if _master_proxy is not None:
        return _master_proxy
    # check against local interpreter plus global env
    master_uri = get_local_master_uri() or env[ROS_MASTER_URI]
    _master_proxy = MasterProxy(master_uri)
    return _master_proxy
getMaster = get_master

#########################################################
# Topic helpers

## \ingroup clientapi 
def get_published_topics(namespace='/'):
    code, msg, val = get_master().getPublishedTopics(namespace)
    if code != 1:
        raise ROSException("unable to get published topics: %s"%msg)
    return val

    
#########################################################
# Service helpers

## \ingroup clientapi 
## Blocks until service is available. Use this in
## initialization code if your program depends on a
## service already running.
## @param service str: name of service
## @param timeout double: timeout time in seconds
## @throws ROSException if specified \a timeout is exceeded
def wait_for_service(service, timeout=None):
    def contact_service(service, timeout=10.0):
        code, _, uri = master.lookupService(service)
        if False and code == 1:
            return True
        elif True and code == 1:
            # disabling for now as this is causing socket.error 22 "Invalid argument" on OS X
            addr = parse_rosrpc_uri(uri)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)            
            try:
                # we always want to timeout just in case we're connecting
                # to a down service.
                s.settimeout(timeout)
                s.connect(addr)
                h = "probe=1\nmd5sum=*\ncallerid=%s\nservice=%s\n"%(get_caller_id(), service)
                s.sendall(struct.pack('<I', len(h)) + h)
                return True
            finally:
                if s is not None:
                    s.close()

    service = resolve_name(service)
    master = get_master()
    first = False
    if timeout:
        timeout_t = time.time() + timeout
        while not is_shutdown() and time.time() < timeout_t:
            try:
                if contact_service(service, timeout_t-time.time()):
                    return
                time.sleep(0.3)
            except: # service not actually up
                if first:
                    first = False
                    logerr("wait_for_service(%s): failed to contact [%s], will keep trying"%(service, uri))
        raise ROSException("timeout exceeded while waiting for service %s"%service)
    else:
        while not is_shutdown():
            try:
                if contact_service(service):
                    return
                time.sleep(0.3)
            except: # service not actually up
                if first:
                    first = False
                    logerr("wait_for_service(%s): failed to contact [%s], will keep trying"%(service, uri))
    
#########################################################
# Param Server Access

_param_server = None
## @internal
## Initialize parameter server singleton
def _init_param_server():
    global _param_server
    if _param_server is None:
        _param_server = get_master() #in the future param server will be a service
        
## \ingroup clientapi 
## Retrieve a parameter from the param server
## @return XmlRpcLegalValue: parameter value
## @throws ROSException if parameter server reports an error
## @throws KeyError if value not set        
def get_param(param_name):
    _init_param_server()
    return _param_server[param_name] #MasterProxy does all the magic for us

## \ingroup clientapi 
## Retrieve list of parameter names
## @return [str]: parameter names
## @throws ROSException if parameter server reports an error
def get_param_names():
    _init_param_server()
    code, msg, val = _param_server.getParamNames() #MasterProxy does all the magic for us
    if code != 1:
        raise ROSException("Unable to retrieve parameter names: %s"%msg)
    else:
        return val

## \ingroup clientapi 
## Set a parameter on the param server
## @param param_name str: parameter name
## @param param_value XmlRpcLegalValue: parameter value
## @throws ROSException if parameter server reports an error
def set_param(param_name, param_value):
    _init_param_server()
    _param_server[param_name] = param_value #MasterProxy does all the magic for us

## \ingroup clientapi 
## Delete a parameter on the param server
## @param param_name str: parameter name
## @throws ROSException if parameter server reports an error
def delete_param(param_name):
    _init_param_server()
    del _param_server[param_name] #MasterProxy does all the magic for us

## \ingroup clientapi 
## Test if parameter exists on the param server
## @param param_name str: parameter name
## @throws ROSException if parameter server reports an error
def has_param(param_name):
    _init_param_server()
    return param_name in _param_server #MasterProxy does all the magic for us

