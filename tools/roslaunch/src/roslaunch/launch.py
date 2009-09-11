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

import os
import subprocess
import sys
import time
import traceback

import roslib.names
import roslib.network 

from roslaunch.core import *
from roslaunch.config import ROSLaunchConfig
from roslaunch.nodeprocess import *
from roslaunch.pmon import start_process_monitor

_TIMEOUT_MASTER_START = 10.0 #seconds
_TIMEOUT_MASTER_STOP  = 10.0 #seconds

_ID = '/roslaunch'

## Runs a roslaunch. The normal sequence of API calls is launch()
## followed by spin(). An external thread can call stop(); otherwise
## the runner will block until an exit signal. Another usage is to
## call launch() followed by repeated calls to spin_once(). This usage
## allows the main thread to continue to do work while processes are
## monitored.
class ROSLaunchRunner(object):
    
    ## @param self
    ## @param config ROSLaunchConfig: roslauch instance to run
    ## @param server_uri str: XML-RPC URI of roslaunch server. If set,
    ## roslaunch is a child process. If not set, this runner is the
    ## server
    ## @param pmon ProcessMonitor: optionally override the process
    ## monitor the runner uses for starting and tracking processes
    ## @param is_core bool: if True, this runner is a roscore instance. This
    ## affects the error behavior if a master is already running.
    def __init__(self, config, server_uri=None, pmon=None, is_core=False):
        self.config = config
        self.server_uri = server_uri
        self.is_core = is_core
        import logging
        self.logger = logging.getLogger('roslaunch')
        self.pm = pmon or start_process_monitor()        
        self.remote_runner = None
                
    ## Load parameters onto the parameter server
    ## @param self
    def _load_parameters(self):
        self.logger.info("load_parameters starting ...")
        config = self.config
        param_server = config.master.get()
        try:
            for p in config.clear_params:
                if param_server.hasParam(_ID, p)[2]:
                    print "deleting parameter [%s]"%p
                    code, msg, _ = param_server.deleteParam(_ID, p)
                    if code != 1:
                        print >> sys.stderr, "Failed to delete parameter [%s]"%p, msg
            for p in config.params.itervalues():
                print "setting parameter [%s]"%p.key
                code, msg, _ = param_server.setParam(_ID, p.key, p.value)
                if code != 1:
                    print >> sys.stderr, "Failed to set parameter [%s] to [%s]"%(p.key, p.value)
        except Exception, e:
            self.logger.error("load_parameters: unable to set parameters: %s", traceback.format_exc())
        self.logger.info("... load_parameters complete")            

    ## Launch all the declared nodes/master
    ## @param self
    ## @return [[str], [str]]: two lists of node names where the first
    ## is the nodes that successfully launched and the second is the
    ## nodes that failed to launch.
    def _launch_nodes(self):
        config = self.config
        succeeded = []
        failed = []
        self.logger.info("launch_nodes: launching local nodes ...")
        local_nodes = config.nodes

        # don't launch remote nodes
        local_nodes = [n for n in config.nodes if is_machine_local(n.machine)]
            
        for node in local_nodes:
            name, success = self._launch_node(node)
            if success:
                succeeded.append(name)
            else:
                failed.append(name)

        if self.remote_runner:
            self.logger.info("launch_nodes: launching remote nodes ...")
            r_succ, r_fail = self.remote_runner.launch_remote_nodes()
            succeeded.extend(r_succ)
            failed.extend(r_fail)            
                
        self.logger.info("... launch_nodes complete")
        return succeeded, failed

    ## Validates master configuration and changes the master URI if
    ## necessary. Also shuts down any existing master.
    ## @param self
    ## @throws RLException if existing master cannot be killed
    def _setup_master(self):
        m = self.config.master
        self.logger.info("initial ROS_MASTER_URI is %s", m.uri)     
        if m.auto in [m.AUTO_START, m.AUTO_RESTART]:
            running = m.is_running() #save state as calls are expensive
            if m.auto == m.AUTO_RESTART and running:
                print "shutting down existing master"
                try:
                    m.get().shutdown(_ID, 'roslaunch restart request')
                except:
                    pass
                timeout_t = time.time() + _TIMEOUT_MASTER_STOP
                while m.is_running() and time.time() < timeout_t:
                    time.sleep(0.1)
                if m.is_running():
                    raise RLException("ERROR: could not stop existing master")
                running = False
            if not running:
                # force the master URI to be for this machine as we are starting it locally
                olduri = m.uri
                m.uri = remap_localhost_uri(m.uri, True)

                # this block does some basic DNS checks so that we can
                # warn the user in the _very_ common case that their
                # hostnames are not configured properly
                hostname, _ = roslib.network.parse_http_host_and_port(m.uri)
                local_addrs = roslib.network.get_local_addresses()
                reverse_ip = socket.gethostbyname(hostname)
                if reverse_ip not in local_addrs:
                    self.logger.warn("IP address %s local hostname '%s' not in local addresses (%s)."%(reverse_ip, hostname, ','.join(local_addrs)))
                    print >> sys.stderr, \
"""WARNING: IP address %s for local hostname '%s' does not appear to match
any local IP address (%s). Your ROS nodes may fail to communicate.

Please use ROS_IP to set the correct IP address to use."""%(reverse_ip, hostname, ','.join(local_addrs))

                if m.uri != olduri:
                    self.logger.info("changing ROS_MASTER_URI to [%s] for starting master locally", m.uri)
                    print "changing ROS_MASTER_URI to [%s] for starting master locally"%m.uri

    ## Launches master if requested. Must be run after _setup_master().
    ## @param self
    ## @throws RLException if master launch fails
    def _launch_master(self):
        m = self.config.master
        auto = m.auto
        is_running = m.is_running()
        if self.is_core and is_running:
            raise RLException("roscore cannot run as another roscore/master is already running. \nPlease kill other roscore/zenmaster processes before relaunching")

        self.logger.debug("launch_master [%s]", auto)
        if auto in [m.AUTO_START, m.AUTO_RESTART] and not is_running:
            if auto == m.AUTO_START:
                self.logger.info("starting new master (master configured for auto start)")
                print "starting new master (master configured for auto start)"
            elif auto == m.AUTO_RESTART:
                self.logger.info("starting new master (master configured for auto restart)")
                print "starting new master (master configured for auto restart)"
                
            _, urlport = roslib.network.parse_http_host_and_port(m.uri)
            if urlport <= 0:
                raise RLException("ERROR: master URI is not a valid XML-RPC URI. Value is [%s]"%m.uri)

            p = create_master_process(m.type, get_ros_root(), urlport, m.log_output)
            self.pm.register_core_proc(p)
            success = p.start()
            if not success:
                raise RLException("ERROR: unable to auto-start master process")
            timeout_t = time.time() + _TIMEOUT_MASTER_START
            while not m.is_running() and time.time() < timeout_t:
                time.sleep(0.1)

        if not m.is_running():
            raise RLException("ERROR: could not contact master [%s]"%m.uri)

        # #773: unique run ID
        param_server = m.get()
        code, _, val = param_server.hasParam('/roslaunch', '/run_id')
        if code == 1 and not val:
            try:
                import uuid
            except ImportError, e:
                import roslib.uuid as uuid
            run_id = str(uuid.uuid1())
            printlog_bold("setting /run_id to %s"%run_id)
            param_server.setParam('/roslaunch', '/run_id', run_id)

    ## Launch a single Executable object. Blocks until executable finishes.
    ## @param e Executable
    ## @throws RLException if exectuable fails. Failure includes non-zero exit code.
    def _launch_executable(self, e):
        try:
            #kwc: I'm still debating whether shell=True is proper
            cmd = e.command
            if isinstance(e, RosbinExecutable):
                cmd = os.path.join(get_ros_root(), 'bin', cmd)
            cmd = "%s %s"%(cmd, ' '.join(e.args))
            print "running %s"%cmd
            retcode = subprocess.call(cmd, shell=True)
            if retcode < 0:
                raise RLException("command [%s] failed with exit code %s"%(cmd, retcode))
        except OSError, e:
            raise RLException("command [%s] failed: %s"%(cmd, e))
        
    #TODO: _launch_run_executables, _launch_teardown_executables
    #TODO: define and implement behavior for remote launch
    ## @throws RLException if exectuable fails. Failure includes non-zero exit code.
    def _launch_setup_executables(self):
        exes = [e for e in self.config.executables if e.phase == PHASE_SETUP]
        for e in exes:
            self._launch_executable(e)
    
    ## launch any core services that are not already running. master must
    ## be already running
    ## @param self
    ## @throws RLException if core launches fail
    def _launch_core_nodes(self):
        config = self.config
        master = config.master.get()
        tolaunch = []
        for node in config.nodes_core:
            node_name = roslib.names.ns_join(node.namespace, node.name)
            code, msg, _ = master.lookupNode(_ID, node_name)
            if code == -1:
                tolaunch.append(node)
            elif code == 1:
                print "core service [%s] is already running, will not launch"%node_name
            else:
                print >> sys.stderr, "WARN: master is not behaving well (unexpected return value when looking up node)"
                self.logger.error("ERROR: master return [%s][%s] on lookupNode call"%(code,msg))
                
        for node in tolaunch:
            node_name = roslib.names.ns_join(node.namespace, node.name)
            name, success = self._launch_node(node, core=True)
            if success:
                print "started core service [%s]"%node_name
            else:
                raise RLException("failed to start core service [%s]"%node_name)

    ## Launch a single node locally. Remote launching is handled separately by the remote module.
    ## @param self
    ## @param node Node: node to launch
    ## @param core bool: if True, core node
    ## @return str, bool: node process name, successful launch
    def _launch_node(self, node, core=False):
        self.logger.debug("... preparing to launch node of type [%s/%s]", node.package, node.type)
        master = self.config.master
        try:
            process = create_node_process(node, master.uri)
        except NodeParamsException, e:
            if node.package == 'rosout' and node.type == 'rosout':
                print >> sys.stderr, "\n\n\nERROR: rosout is not built. Please run 'rosmake rosout'\n\n\n"
            else:
                print >> sys.stderr, "ERROR: cannot launch node of type [%s/%s]: %s"%(node.package, node.type, str(e))
            if node.name:
                return node.name, False
            else:
                return "%s/%s"%(node.package,node.type), False                

        self.logger.debug("... created process [%s]", process.name)
        if core:
            self.pm.register_core_proc(process)
        else:
            self.pm.register(process)            
        node.process_name = process.name #store this in the node object for easy reference
        self.logger.debug("... registered process [%s]", process.name)            
        success = process.start()
        if not success:
            print "launch of %s/%s on %s failed"%(node.package, node.type, node.machine.name)
            self.logger.info("launch of %s/%s on %s failed"%(node.package, node.type, node.machine.name))
        else:
            self.logger.debug("... successfully launched [%s]", process.name)
        return process.name, success
        
    ## Check for running node process.
    ## @param node Node: node object to check
    ## @return bool: True if process associated with node is running (launched && !dead)
    def is_node_running(self, node):
        #process_name is not set until node is launched.
        return node.process_name and self.pm.has_process(node.process_name)
    
    ## same as spin() but only does one cycle. must be run from the main thread.
    def spin_once(self):
        if not self.pm:
            return False
        return self.pm.mainthread_spin_once()
        
    ## spin() must be run from the main thread. spin() is very
    ## important for roslaunch as it picks up jobs that the process
    ## monitor need to be run in the main thread.
    ## @param self
    def spin(self):
        self.logger.info("spin")

        # #556: if we're just setting parameters and aren't launching
        # any processes, exit.
        if not self.pm or not self.pm.get_active_names():
            printlog_bold("No processes to monitor")
            self.stop()
            return # no processes
        self.pm.mainthread_spin()
        #self.pm.join()
        self.logger.info("process monitor is done spinning, initiating full shutdown")
        self.stop()
        printlog_bold("done")
    
    ## Stop the launch and all associated processes. not thread-safe.
    ## @param self
    def stop(self):
        if self.pm is not None:
            printlog("shutting down processing monitor...")
            self.pm.shutdown()
            self.pm.join()
            self.pm = None
            printlog("... shutting down processing monitor complete")

    ## setup the state of the ROS network, including the parameter
    ## server state and core services
    ## @param self
    def _setup(self):
        config = self.config
        # make sure our environment is correct
        config.validate()

        # choose machines for the nodes 
        config.assign_machines()
        print config.summary()

        # have to do setup on mster before launching remote roslaunch
        # children as we may be changing the ROS_MASTER_URI.
        self._setup_master()

        if config.has_remote_nodes():
            ## keep the remote package lazy-imported
            import remote
            self.remote_runner = remote.ROSRemoteRunner(config, self.pm)
            self.remote_runner.setup()

        # start up the core: master + core nodes defined in core.xml
        self._launch_master()
        self._launch_core_nodes()        
        
        # no parameters for a child process
        if not self.server_uri:
            self._load_parameters()

        # run exectuables marked as setup period. this will block until
        # these exectuables exit.
        self._launch_setup_executables()
        
    ## Run the launch. Depending on usage, caller should call
    ## spin_once or spin as appropriate after launch().
    ## @param self
    ## @return ([str], [str]): tuple containing list of nodes that
    ## successfully launches and list of nodes that failed to launch
    def launch(self):
        self._setup()        
        succeeded, failed = self._launch_nodes()
        # inform process monitor that we are done with process registration
        self.pm.registrations_complete()
        return succeeded, failed 

    ## Run the test node. Blocks until completion or timeout.
    ## @param self
    ## @param test Test: test node to run    
    ## @raise RLException if test fails to launch or test times out
    def run_test(self, test):
        name, success = self._launch_node(test)
        if not success:
            raise RLException("test [%s] failed to launch"%test.test_name)

        #poll until test terminates or alloted time exceed
        timeout_t = time.time() + test.time_limit
        pm = self.pm
        while pm.mainthread_spin_once() and self.is_node_running(test):
            #test fails on timeout
            if time.time() > timeout_t:
                raise RLException("test max time allotted")
            time.sleep(0.1)
        
