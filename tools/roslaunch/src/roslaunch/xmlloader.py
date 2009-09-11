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
import sys
import xmlrpclib
import traceback

from xml.dom.minidom import parse, parseString
from xml.dom import Node as DomNode
from core import *

from roslib.names import make_global_ns, ns_join, is_global, is_private, PRIV_NAME
from roslib.packages import InvalidROSPkgException
from roslib.substitution_args import resolve_args, SubstitutionException

NS='ns'
CLEAR_PARAMS='clear_params'

class XmlParseException(RLException): pass
class XmlLoadException(RLException): pass

class _XmlContext(object):
    def __init__(self, ns, parent=None):
        self.parent = parent
        self.ns = make_global_ns(ns or '/')
        self._remap_args = []
    ## add a new remap setting to the context
    def add_remap(self, remap):
        self._remap_args.append(remap)
    ## get a copy of the current remap arguments
    def remap_args(self):
        if self.parent:
            return self._remap_args + self.parent.remap_args()
        return self._remap_args[:]
    ## @param ns str: sub-namespace of child context, or None if the
    ##   child context shares the same namespace
    ## @return A child xml context that inherits from this context
    def child(self, ns):
        if ns:
            if ns[0] == '/': # global (discouraged)
                return _XmlContext(ns, self)
            elif ns == PRIV_NAME: # ~name
                # private names can only be scoped privately or globally
                return _XmlContext(PRIV_NAME, self)
            else:
                return _XmlContext(ns_join(self.ns, ns), self)
        else:
            return _XmlContext(self.ns, self)

## helper routine for fetching and resolving optional tag attributes
## @param tag DOM tag
## @param attrs (str): list of attributes to resolve        
def opt_attrs(tag, attrs):
    def tag_value(tag, a):
        if tag.hasAttribute(a):
            # getAttribute returns empty string for non-existent
            # attributes, which makes it impossible to distinguish
            # with actual empty values
            return tag.getAttribute(a)
        else:
            return None
    return [resolve_args(tag_value(tag,a)) for a in attrs]

## helper routine for fetching and resolving required tag attributes
## @param tag DOM tag
## @param attrs (str): list of attributes to resolve        
## @raise KeyError if required attribute is missing
def reqd_attrs(tag, attrs):
    return [resolve_args(tag.attributes[a].value) for a in attrs]

class XmlLoader(object):
    def __init__(self): pass

    def _check_attrs(self, tag, attrs):
        tag_attrs = tag.attributes.keys()
        for t_a in tag_attrs:
            if not t_a in attrs:
                print >> sys.stderr, "WARNING: unknown <%s> attribute '%s'"%(tag.tagName, t_a)

    MASTER_ATTRS = ('type', 'uri', 'auto')
    def _master_tag(self, tag):
        self._check_attrs(tag, XmlLoader.MASTER_ATTRS)
        type, uri, auto_str = opt_attrs(tag, ('type', 'uri', 'auto'))
        if tag.hasAttribute('type') and not len(type.strip()):
            raise XmlParseException("<master> 'type' must be non-empty")
        auto_str = (auto_str or 'no').lower().strip()
        if auto_str == 'no':
            auto = Master.AUTO_NO
        elif auto_str == 'start':
            auto = Master.AUTO_START
        elif auto_str == 'restart':
            auto = Master.AUTO_RESTART
        else:
            raise XmlParseException("invalid <master> 'auto' value: %s"%auto_str)
        try:
            return Master(type_=type, uri=uri, auto=auto)
        except ValueError, e:
            raise XmlParseException("invalid <master> tag: %s"%str(e))

    # rosparam tag also has optional 'ns' attribute
    ROSPARAM_ATTRS = ('file', 'command')
    def _rosparam_tag(self, tag, context, ros_config):
        try:
            file, arg = reqd_attrs(tag, XmlLoader.ROSPARAM_ATTRS)
            if not file:
                raise XmlParseException("<rosparam> 'file' attribute must be non-empty")
            if not arg in ('load', 'dump'):
                raise XmlParseException("<rosparam> 'command' must be 'load' or 'dump'")
            
            child_ns = self._ns_clear_params_attr('rosparam', tag, context, ros_config)
            #TODO: enable rosparam to run during any phase
            return RosbinExecutable('rosparam', (arg, file, child_ns.ns), PHASE_SETUP)
        except KeyError, e:
            raise XmlParseException(
                "<rosparam> tag is missing required attribute: %s. rosparam xml is %s"%(e, tag.toxml()))

    PARAM_ATTRS = ('name', 'value', 'type', 'value', 'textfile', 'binfile', 'command')
    def _param_tag(self, tag, context):
        try:
            self._check_attrs(tag, XmlLoader.PARAM_ATTRS)
            name = resolve_args(tag.attributes['name'].value.strip())
            if is_private(name):
                raise XmlParseException(
                    "<param> tags may not use ~names. Place the <param> tag within a <node> tag instead.")
            name  = ns_join(context.ns, name)
            ptype = (tag.getAttribute('type') or 'auto').lower().strip()
            
            vals = opt_attrs(tag, ('value', 'textfile', 'binfile', 'command'))
            if len([v for v in vals if v is not None]) != 1:
                raise XmlParseException(
                    "<param> tag must have one and only one of value/textfile/binfile.")
                
            value, textfile, binfile, command = vals
            if value is not None:
                value = convert_value(value.strip(), ptype)
            elif textfile is not None:
                f = open(textfile, 'r')
                try:
                    value = f.read()
                finally:
                    f.close()
            elif binfile is not None:
                f = open(binfile, 'rb')
                try:
                    value = xmlrpclib.Binary(f.read())
                finally:
                    f.close()
            elif command is not None:
                if type(command) == unicode:
                  command = command.encode('UTF-8') #attempt to force to string for shlex/subprocess
                print "... executing command param [%s]"%command
                import subprocess, shlex #shlex rocks
                try:
                    p = subprocess.Popen(shlex.split(command), stdout=subprocess.PIPE)
                    c_value = p.communicate()[0]
                    value = c_value
                    if p.returncode != 0:
                        raise XmlLoadException("Cannot load command parameter [%s]: command [%s] returned with code [%s]"%(name, command, p.returncode))
                except OSError, (errno, strerr):
                    if errno == 2:
                        raise XmlLoadException("Cannot load command parameter [%s]: no such command [%s]"%(name, command))
                    raise
                if value is None:
                    raise XmlLoadException("parameter: unable to get output of command [%s]"%command)
            return Param(name, value)
        except KeyError, e:
            raise XmlParseException(
                "<param> tag is missing required attribute: %s. \n\nParam xml is %s"%(e, tag.toxml()))
        except XmlParseException, e:
            raise XmlParseException(
                "%s. \n\nParam xml is %s"%(e, tag.toxml()))
        except SubstitutionException, e:
            raise XmlParseException(
                "%s. \n\nParam xml is %s"%(e, tag.toxml()))
        except RLException, e:
            raise XmlParseException(
                "%s. \n\nParam xml is %s"%(e, tag.toxml()))
        except XmlLoadException, e:
            raise
        except Exception, e:
            raise XmlParseException(
                "Invalid <param> tag: %s. \n\nParam xml is %s"%(e, tag.toxml()))

    NODE_ATTRS = ['pkg', 'type', 'machine', 'name', 'args', 'output', 'respawn', 'cwd', NS, CLEAR_PARAMS]
    TEST_ATTRS = NODE_ATTRS + ['test-name','time-limit']
    
    ## @param tag Node: DOM node
    ## @param context _XmlContext: namespace context
    ## @param params [Param]: ROS parameter list
    ## @param clear_params [str]: list of ROS parameter names to clear before setting parameters
    ## @param default_machine str: default machine to assign to node
    ## @param is_test bool: if set, will load as Test object instead
    ## of Node object
    def _node_tag(self, tag, context, ros_config, default_machine, is_test=False):
        try:
            if is_test:
                self._check_attrs(tag, XmlLoader.TEST_ATTRS)
            else:
                self._check_attrs(tag, XmlLoader.NODE_ATTRS)
                
            # required attributes
            pkg, node_type = reqd_attrs(tag, ('pkg', 'type'))
            
            if not len(pkg.strip()):
                raise XmlParseException("<node> 'pkg' must be non-empty")
            if not len(node_type.strip()):
                raise XmlParseException("<node> 'type' must be non-empty")
            
            # optional attributes
            machine, name, args, output, respawn, cwd = \
                     opt_attrs(tag, ('machine', 'name', 'args', 'output', 'respawn', 'cwd'))
            child_ns = self._ns_clear_params_attr('node', tag, context, ros_config, node_name=name)

            if tag.hasAttribute('machine') and not len(machine.strip()):
                raise XmlParseException("<node> 'machine' must be non-empty: [%s]"%machine)
            if not machine and default_machine:
                machine = default_machine.name
            # valid values are 'log' or 'screen'
            output = output or 'log'
            if not output in ['log', 'screen']:
                raise XmlParseException("<%s> 'output' attribute must be one of: 'log', 'screen'"%tag.tagName)

            respawn = (respawn or "false").lower()
            if respawn == 'true':
                respawn = True
            elif respawn == 'false':
                respawn = False
            else:
                raise XmlParseException("Invalid respawn value: %s"%respawn)

            valid_cwd = ['ros-root', 'node']
            if cwd and cwd not in valid_cwd:
                raise XmlParseException("<%s> 'cwd' attribute must be one of: %s"%(tag.tagName, ','.join(valid_cwd)))

            # each node gets its own copy of <remap> arguments, which
            # it inherits from its parent
            remap_args = context.remap_args()
            # nodes can have individual env args set in addition to
            # the ROS-specific ones.  we should consider simplifying
            # implementation and stuffing the ROS-specific ones in the
            # same data structure.
            env_args = []
            for t in [c for c in tag.childNodes if c.nodeType == DomNode.ELEMENT_NODE]:
                tagName = t.tagName.lower()
                if tagName == 'remap':
                    remap_args.append(self._remap_tag(t))
                elif tagName in ['param', 'rosparam']:
                    if not name:
                        raise XmlParseException(
                            "<node> tag must have a 'name' attribute in order to use parameter tags: %s"%t.toxml())
                    if child_ns is not None:
                        param_ns = child_ns
                    else:
                        param_ns = context
                    param_ns = param_ns.child(name)
                    if tagName == 'param':
                        ros_config.add_param(self._param_tag(t, param_ns))
                    elif tagName == 'rosparam':
                        ros_config.add_executable(self._rosparam_tag(t, param_ns, ros_config)) 
                elif tagName == 'env':
                    env_args.append(self._env_tag(t))
                else:
                    print >> sys.stderr, \
                          "WARN: unrecognized '%s' tag in <node> tag"%t.tagName

            if not is_test:
                return Node(pkg, node_type, name=name, namespace=child_ns.ns, machine_name=machine, \
                            args=args, respawn=respawn, remap_args=remap_args, env_args=env_args,\
                            output=output, cwd=cwd)
            else:
                if respawn:
                    raise XmlParseException(
                        "<test> tags may not have respawn attributes")
                test_name = resolve_args(tag.attributes['test-name'].value)
                time_limit = resolve_args(tag.getAttribute('time-limit'))
                if time_limit:
                    if '.' in time_limit:
                        time_limit = string.atof(time_limit)
                    else:
                        #force to floating point
                        time_limit = float(string.atoi(time_limit) )
                    if time_limit <= 0.0:
                        raise XmlParseException("'time-limit' must be a positive number")
                return Test(test_name, pkg, node_type, name=name, namespace=child_ns.ns, \
                            machine_name=machine, args=args, remap_args=remap_args, env_args=env_args,\
                            time_limit=time_limit, cwd=cwd)
        except KeyError, e:
            if is_test:
                raise XmlParseException(
                    "<test> tag is missing required attribute: %s. Node xml is %s"%(e, tag.toxml()))
            else:
                raise XmlParseException(
                    "<node> tag is missing required attribute: %s. Node xml is %s"%(e, tag.toxml()))
        except SubstitutionException, e:
            raise XmlParseException(
                "%s. \n\nParam xml is %s"%(e, tag.toxml()))
        except RLException, e:
            raise XmlParseException(
                "%s. \n\nNode xml is %s"%(e, tag.toxml()))
        except XmlParseException, e:
            raise XmlParseException(
                "%s. \n\nNode xml is %s"%(e, tag.toxml()))

    MACHINE_ATTRS = ('name', 'address', 'ros-root', 'ros-package-path', 'ros-ip', 'ros-host-name', 
                     'ssh-port', 'user', 'password', 'default')
    def _machine_tag(self, tag):
        try:
            self._check_attrs(tag, XmlLoader.MACHINE_ATTRS)
            # required attributes
            name, address = reqd_attrs(tag, ('name', 'address'))
            
            # optional attributes
            rosroot, ros_package_path, ros_ip, ros_host_name, ssh_port, user, password, default = \
                     opt_attrs(tag, \
                               ('ros-root', 'ros-package-path', 'ros-ip', 'ros-host-name', \
                                'ssh-port', 'user', 'password', 'default'))

            if ros_host_name and ros_ip:
                raise XmlParseException("only one of 'ros-host-name' or 'ros-ip' may be set")
            ros_host_name = ros_host_name or ros_ip  #alias
            
            if not rosroot:
                try:
                    rosroot = os.environ['ROS_ROOT']
                except KeyError, e:
                    pass
            if not ros_package_path:
                ros_package_path = get_ros_package_path()
            ssh_port = int(ssh_port or '22')
            # check for default switch
            default = (default or 'false').lower()
            if not default in ['true', 'false', 'never']:
                raise XmlParseException("Invalid value for 'attribute': %s"%default)
            if default == 'never':
                assignable = False
                is_default = False
            else:
                assignable = True
                if default == 'true':
                    is_default = True
                else:
                    is_default = False                    
                
            m =  Machine(name, rosroot, ros_package_path, address, \
                         ros_ip=ros_host_name, ssh_port=ssh_port, user=user, password=password, \
                         assignable=assignable)
            return (m, is_default)
        except KeyError, e:
            raise XmlParseException("<machine> tag is missing required attribute: %s"%e)
        except SubstitutionException, e:
            raise XmlParseException(
                "%s. \n\nParam xml is %s"%(e, tag.toxml()))
        except RLException, e:
            raise XmlParseException(
                "%s. \n\nMachine xml is %s"%(e, tag.toxml()))
        
    REMAP_ATTRS = ('from', 'to')
    def _remap_tag(self, tag):
        try:
            self._check_attrs(tag, XmlLoader.REMAP_ATTRS)
            return reqd_attrs(tag, XmlLoader.REMAP_ATTRS)
        except KeyError, e:
            raise XmlParseException("<remap> tag is missing required from/to attributes: %s"%tag.toxml())
        
    ENV_ATTRS = ('name', 'value')
    def _env_tag(self, tag):
        try:
            self._check_attrs(tag, XmlLoader.ENV_ATTRS)
            name, value = reqd_attrs(tag, XmlLoader.ENV_ATTRS)
            if not name:
                raise XmlParseException("<env> 'name' attribute must be non-empty: %s"%tag.toxml())                
            return name, value
        except KeyError, e:
            raise XmlParseException("<env> tag is missing required name/value attributes: %s"%tag.toxml())
    
    ## common processing routine for xml tags with NS and CLEAR_PARAMS attributes
    ## @param tag Node: DOM Node
    ## @param context _XmlContext: current namespace context 
    ## @param clear_params [str]: list of params to clear
    ## @param node_name str: name of node (for use when \a tagName == 'node')
    ## @return _XmlContext namespace context 
    def _ns_clear_params_attr(self, tagName, tag, context, ros_config, node_name=None):
        if tag.hasAttribute(NS):
            ns = resolve_args(tag.getAttribute(NS))
            if not ns:
                raise XmlParseException("<%s> tag has an empty '%s' attribute"%(tagName, NS))
        else:
            ns = None
        child_ns = context.child(ns)
        clear_p = resolve_args(tag.getAttribute(CLEAR_PARAMS))
        if clear_p:
            clear_p = clear_p.lower()
            if not clear_p in ['true', 'false']:
                raise XmlParseException("'clear_params' attribute must be set to true or false")
            if clear_p == 'true':
                if tagName == 'node':
                    if not node_name:
                        raise XmlParseException("<%s> tag must have a 'name' attribute to use '%s' attribute"%(tagName, CLEAR_PARAMS))
                    # use make_global_ns to give trailing slash in order to be consistent with XmlContext.ns
                    ros_config.add_clear_param(make_global_ns(ns_join(child_ns.ns, node_name)))
                else:
                    if not ns:
                        raise XmlParseException("'ns' attribute must be set in order to use 'clear_params'")                
                    ros_config.add_clear_param(child_ns.ns)
        return child_ns
        
    INCLUDE_ATTRS = ('file', NS, CLEAR_PARAMS)
    GROUP_ATTRS = (NS, CLEAR_PARAMS)
    ## @return default_machine Machine new default machine for current context
    def _recurse_load(self, ros_config, tags, context, default_machine, is_core):
        for tag in [t for t in tags if t.nodeType == DomNode.ELEMENT_NODE]:
            name = tag.tagName
            if name == 'group':
                self._check_attrs(tag, XmlLoader.GROUP_ATTRS)
                try:
                    child_ns = self._ns_clear_params_attr(name, tag, context, ros_config)
                    default_machine = \
                                    self._recurse_load(ros_config, tag.childNodes, child_ns, \
                                                       default_machine, is_core)
                except KeyError, e:
                    raise XmlParseException("<ns> tag is missing required 'name' attribute")
            elif name == 'node':
                ros_config.add_node(self._node_tag(tag, context, ros_config, default_machine), core=is_core)
            elif name == 'test':
                ros_config.add_test(self._node_tag(tag, context, ros_config, default_machine, is_test=True))
            elif name == 'param':
                ros_config.add_param(self._param_tag(tag, context))
            elif name == 'remap':
                context.add_remap(self._remap_tag(tag))
            elif name == 'machine':
                (m, is_default) = self._machine_tag(tag)
                if is_default:
                    default_machine = m
                ros_config.add_machine(m)
            elif name == 'rosparam':
                ros_config.add_executable(self._rosparam_tag(tag, context, ros_config))
            elif name == 'master':
                pass #handled non-recursively
            elif name == 'include':
                self._check_attrs(tag, XmlLoader.INCLUDE_ATTRS)
                filename = resolve_args(tag.attributes['file'].value)
                child_ns = self._ns_clear_params_attr(name, tag, context, ros_config)
                launch = self._parse_launch(filename)
                default_machine = \
                                self._recurse_load(ros_config, launch.childNodes, child_ns, \
                                                   default_machine, is_core)
                print "... done importing include file [%s]"%filename                
            else:
                logger.getLogger('roslaunch').warn("WARNING: unrecognized XML tag", tag.tagName)
                print "WARNING: unrecognized tag", tag.tagName
        return default_machine

    ## subroutine of launch for loading XML DOM into config
    ## @param launch Node: DOM node of the root <launch> tag in the file
    ## @param ros_config ROSLaunchConfig: launch configuration to load XML file into
    ## @param is_core bool: if True, load file using ROS core rules
    def _load_launch(self, launch, ros_config, is_core=False):
        # The <master> tag is special as we only only process a single
        # tag in the top-level file. We ignore master tags in
        # included files.
        master_tags = launch.getElementsByTagName('master')
        if len(master_tags) > 1:
            raise XmlParseException("multiple <master> tags in top-level xml file not allowed")
        elif len(master_tags) == 1:
            ros_config.set_master(self._master_tag(master_tags[0]))
        self._recurse_load(ros_config, launch.childNodes, _XmlContext(''), None, is_core)
        
    def _parse_launch(self, filename):
        try:
            print "... loading XML file [%s]"%filename
            return parse(filename).getElementsByTagName('launch')[0]
        except Exception, e:
            raise XmlParseException("Invalid roslaunch XML syntax: %s"%e)
        
    ## load XML file into launch configuration
    ## @param filename str: XML config file to load
    ## @param ros_config ROSLaunchConfig: launch configuration to load XML file into
    ## @param core bool: if True, load file using ROS core rules
    def load(self, filename, ros_config, core=False):
        launch = self._parse_launch(filename)
        self._load_launch(launch, ros_config, is_core=core)

    ## load XML text into launch configuration
    ## @param xml_text str: XML configuration
    ## @param ros_config ROSLaunchConfig: launch configuration to load XML file into
    ## @param core bool: if True, load file using ROS core rules
    def load_string(self, xml_text, ros_config, core=False):
        try:
            print "... loading XML"
            launch = parseString(xml_text).getElementsByTagName('launch')[0]
        except Exception, e:
            logger.getLogger('roslaunch').error("Invalid roslaunch XML syntax: %s"%traceback.format_exc())
            raise XmlParseException("Invalid roslaunch XML syntax: %s"%e)
        self._load_launch(launch, ros_config, core)
