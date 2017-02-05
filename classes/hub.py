import pprint
import logging
from classes import *
import device
import struct
import time
import binascii
import threading

pp = pprint.PrettyPrinter(indent=4)

class Hub(Base):

    def __init__(self, serialObj = False):
        Base.__init__(self, serialObj)

        # Type Info
        self.manu = 'AlertMe.com'
        self.type = 'Nano Hub'
        self.date = '2017-01-01'
        self.version = 00001

        # List of associated nodes
        self.nodes = []

    def set_node_attributes(self, node_index, attributes):
        for attribute, value in attributes.iteritems():
            self.set_node_attribute(node_index, attribute, value)

    def set_node_attribute(self, node_index, attribute, value):
        if not self.nodes[node_index]['attributes'].has_key(attribute):
            self.nodes[node_index]['attributes'][attribute] = {}
        self.nodes[node_index]['attributes'][attribute]['reportedValue'] = value
        self.nodes[node_index]['attributes'][attribute]['reportReceivedTime'] = int(time.time())

    def list_nodes(self):
        return self.nodes

    def rename_node(self, node_index, name):
        self.nodes[node_index]['name'] = name

    def command(self, node_index, attribute, value):
        # Work out Zigbee addresses
        dest_addr_long  = self.nodes[node_index]['addrLong']
        dest_addr_short = self.nodes[node_index]['addrShort']

        # Basic message details
        message = {
            'src_endpoint': b'\x00',
            'dest_endpoint': b'\x02',
            'profile': self.ALERTME_PROFILE_ID,
        }

        # Construct message data
        if attribute == 'state':
            message['data'] = b'\x00\xee'
            message = {
                'src_endpoint': b'\x00',
                'dest_endpoint': b'\x02',
                'cluster': b'\x00\xee',
                'profile': self.ALERTME_PROFILE_ID,
            }
            if value == 'ON':
                message['data'] = b'\x11\x00\x02\x01\x01'
            if value == 'OFF':
                message['data'] = b'\x11\x00\x02\x00\x01'

        # Send message
        self.send_message(message, dest_addr_long, dest_addr_short)

    def discovery(self):
        self.logger.debug('Discovery')
        self.thread = threading.Thread(target=self._discovery)

    def _discovery(self):
        # Discovery Phase
        # Send out a broadcast every 3 seconds for a minute
        timeout = time.time() + 60
        while True:
            if time.time() > timeout:
                break

            message = self.get_action('routing_table_request')
            self.send_message(message, self.BROADCAST_LONG, self.BROADCAST_SHORT)

            time.sleep(3.00)

    def process_message(self, message):
        super(Hub, self).process_message(message)

        # We are only interested in Zigbee Explicit packets.
        if (message['id'] == 'rx_explicit'):
            profile_id = message['profile']
            cluster_id = message['cluster']

            source_addr_long = message['source_addr_long']
            source_addr = message['source_addr']

            # Add to list of nodes if not already
            node_id = Base.pretty_mac(source_addr_long)
            node_index = False
            for i, node in enumerate(self.nodes):
                if node['id'] == node_id:
                    node_index = i

            if node_index is False:
                self.nodes.append({
                    'id': node_id,
                    'addrLong': source_addr_long,
                    'addrShort': source_addr,
                    'associated': False,
                    'name': 'Unknown Device',
                    "createdOn": int(time.time()),
                    'lastSeen': int(time.time()),
                    'messagesReceived': 0,
                    'messagesSent': 0,
                    'attributes': {}
                })
                node_index = len(self.nodes) - 1

            self.nodes[node_index]['lastSeen'] = int(time.time())
            self.nodes[node_index]['messagesReceived'] += 1

            if (profile_id == self.ZDP_PROFILE_ID):
                # Zigbee Device Profile ID
                if (cluster_id == b'\x13'):
                    # Device Announce Message.
                    # Due to timing problems with the switch itself, we don't
                    # respond to this message, we save the response for later after the
                    # Match Descriptor request comes in. You'll see it down below.
                    self.logger.debug('Received Device Announce Message')

                elif (cluster_id == b'\x80\x00'):
                    # Possibly Network (16-bit) Address Response.
                    # Not sure what this is? Only seen on the Hive ActivePlug?
                    # See: http://www.desert-home.com/2015/06/hacking-into-iris-door-sensor-part-4.html
                    # http://ftp1.digi.com/support/images/APP_NOTE_XBee_ZigBee_Device_Profile.pdf
                    self.logger.debug('Received Network (16-bit) Address Response')

                elif (cluster_id == b'\x80\x05'):
                    # Active Endpoint Response.
                    # This message tells us what the device can do, but it isn't constructed correctly to match what
                    # the switch can do according to the spec. This is another message that gets it's response after
                    # we receive the Match Descriptor below.
                    self.logger.debug('Received Active Endpoint Response')

                elif (cluster_id == b'\x802'):
                    # Route Record Broadcast Response.
                    self.logger.debug('Received Route Record Broadcast Response')

                elif (cluster_id == b'\x00\x06'):
                    # Match Descriptor Request.
                    self.logger.debug('Received Match Descriptor Request')
                    # This is the point where we finally respond to the switch. Several messages are sent to cause
                    # the switch to join with the controller at a network level and to cause it to regard this
                    # controller as valid.

                    # First send the Active Endpoint Request
                    reply = self.get_action('active_endpoints_request')
                    self.send_message(reply, source_addr_long, source_addr)
                    self.logger.debug('Sent Active Endpoints Request')

                    # Now send the Match Descriptor Response
                    reply = self.get_action('match_descriptor_response')
                    self.send_message(reply, source_addr_long, source_addr)
                    self.logger.debug('Sent Match Descriptor Response')

                    # Now there are two messages directed at the hardware code (rather than the network code).
                    # The switch has to receive both of these to stay joined.
                    reply = self.get_action('hardware_join_1')
                    self.send_message(reply, source_addr_long, source_addr)
                    reply = self.get_action('hardware_join_2')
                    self.send_message(reply, source_addr_long, source_addr)
                    self.logger.debug('Sent Hardware Join Messages')

                    # We are fully associated!
                    # Update nodes to say it is now associated
                    self.nodes[node_index]['associated'] = True
                    self.logger.debug('Device Associated')

                else:
                    self.logger.error('Unrecognised Cluster ID: %e', cluster_id)

            elif (profile_id == self.ALERTME_PROFILE_ID):
                # AlertMe Profile ID

                # Python 2 / 3 hack
                if (hasattr(bytes(), 'encode')):
                    cluster_cmd = message['rf_data'][2]
                else:
                    cluster_cmd = bytes([message['rf_data'][2]])

                if (cluster_id == b'\x00\xee'):
                    if (cluster_cmd == b'\x80'):
                        properties = self.parse_switch_status(message['rf_data'])
                        self.logger.debug('Switch Status: %s', properties)
                        self.set_node_attributes(node_index, properties)
                    else:
                        self.logger.error('Unrecognised Cluster Command: %r', cluster_cmd)

                elif (cluster_id == b'\x00\xef'):
                    if (cluster_cmd == b'\x81'):
                        properties = self.parse_power_info(message['rf_data'])
                        self.logger.debug('Current Instantaneous Power: %s', properties)
                        self.set_node_attributes(node_index, properties)
                    elif (cluster_cmd == b'\x82'):
                        properties = self.parse_usage_info(message['rf_data'])
                        self.logger.debug('Uptime: %s Usage: %s', properties['upTime'], properties['powerConsumption'])
                        self.set_node_attributes(node_index, properties)
                    else:
                        self.logger.error('Unrecognised Cluster Command: %r', cluster_cmd)

                elif (cluster_id == b'\x00\xf0'):
                    if (cluster_cmd == b'\xfb'):
                        properties = self.parse_status_update(message['rf_data'])
                        self.logger.debug('Status Update: %s', properties)
                        self.set_node_attributes(node_index, properties)
                    else:
                        self.logger.error('Unrecognised Cluster Cmd: %r', cluster_cmd)

                elif (cluster_id == b'\x00\xf2'):
                    properties = self.parse_tamper(message['rf_data'])
                    self.logger.debug('Tamper Switch Changed State: %s', properties)
                    self.set_node_attributes(node_index, properties)

                elif (cluster_id == b'\x00\xf3'):
                    properties = self.parse_button_press(message['rf_data'])
                    self.logger.debug('Button Press: %s', properties)
                    self.set_node_attributes(node_index, properties)

                elif (cluster_id == b'\x00\xf6'):
                    if (cluster_cmd == b'\xfd'):
                        properties = self.parse_range_info(message['rf_data'])
                        self.logger.debug('Range Test RSSI Value: %s', properties)
                        self.set_node_attributes(node_index, properties)

                    elif (cluster_cmd == b'\xfe'):
                        properties = self.parse_version_info(message['rf_data'])
                        self.logger.debug('Version Information: %s', properties)
                        self.set_node_attributes(node_index, properties)

                        # We will assume it is also associated
                        self.nodes[node_index]['associated'] = True

                    else:
                        self.logger.error('Unrecognised Cluster Command: %e', cluster_cmd)

                elif (cluster_id == b'\x05\x00'):
                    self.logger.debug('Security Event')
                    # Security Cluster.
                    # When the device first connects, it come up in a state that needs initialization, this command
                    # seems to take care of that. So, look at the value of the data and send the command.
                    if (message['rf_data'][3:7] == b'\x15\x00\x39\x10'):
                        self.logger.debug('Sending Security Initialization')
                        reply = self.get_action('security_initialization')
                        self.send_message(reply, source_addr_long, source_addr)

                    vals = self.parse_security_device(message['rf_data'])
                    self.logger.debug('Security Device Values: %s', vals)

                else:
                    self.logger.error('Unrecognised Cluster ID: %r', cluster_id)

                # Do we know the device type yet?
                if (not self.nodes[node_index]['attributes'].has_key('model')):
                    reply = self.get_action('version_info')
                    self.send_message(reply, source_addr_long, source_addr)
                    self.logger.debug('Sent Type Request')

            else:
                self.logger.error('Unrecognised Profile ID: %r', profile_id)





    @staticmethod
    def parse_version_info(rf_data):
        # The version string is variable length. We therefore have to calculate the
        # length of the string which we then use in the unpack
        l = len(rf_data) - 22
        values = dict(zip(
            ('cluster_cmd', 'hwVersion', 'string'),
            struct.unpack('< 2x s H 17x %ds' % l, rf_data)
        ))

        # Break down the version string into its component parts
        ret = {}
        ret['hwVersion'] = values['hwVersion']
        ret['string']  = str(values['string'].decode())\
            .replace('\t', '\n')\
            .replace('\r', '\n')\
            .replace('\x0e', '\n')\
            .replace('\x0b', '\n')

        ret['manufacturer']    = ret['string'].split('\n')[0]
        ret['model']           = ret['string'].split('\n')[1]
        ret['manufactuerDate'] = ret['string'].split('\n')[2]
        del ret['string']

        return ret

    @staticmethod
    def parse_range_info(rf_data):
        # Parse for RSSI Range Test value
        values = dict(zip(
            ('cluster_cmd', 'RSSI'),
            struct.unpack('< 2x s B 1x', rf_data)
        ))
        rssi = values['RSSI']
        return {'RSSI' : rssi}

    @staticmethod
    def parse_power_info(rf_data):
        # Parse for Current Instantaneous Power value
        values = dict(zip(
            ('cluster_cmd', 'Power'),
            struct.unpack('< 2x s H', rf_data)
        ))
        return {'instantaneousPower' : values['Power']}

    @staticmethod
    def parse_usage_info(rf_data):
        # Parse Usage Stats
        ret = {}
        values = dict(zip(
            ('cluster_cmd', 'powerConsumption', 'upTime'),
            struct.unpack('< 2x s I I 1x', rf_data)
        ))
        ret['powerConsumption'] = values['powerConsumption']
        ret['upTime']           = values['upTime']

        return ret

    @staticmethod
    def parse_switch_status(rf_data):
        # Parse Switch Status
        values = struct.unpack('< 2x b b b', rf_data)
        if (values[2] & 0x01):
            return {'state' : 'ON'}
        else:
            return {'state' : 'OFF'}

    @staticmethod
    def parse_button_press(rf_data):
        ret = {}
        if rf_data[2] == b'\x00':
            ret['state'] = 'OFF'
        elif rf_data[2] == b'\x01':
            ret['state'] = 'ON'
        else:
            ret['state'] = {}

        ret['counter'] = struct.unpack('<H', rf_data[5:7])[0]

        return ret

    @staticmethod
    def parse_status_update(rf_data):
        ret = {}
        status = rf_data[3]
        if (status == b'\x1c'):
            # Power Switch
            ret['Type'] = 'Power Switch'
            # Never found anything useful in this

        elif (status == b'\x1d'):
            # Key Fob
            ret['Type'] = 'Key Fob'
            ret['Temp_F']  = float(struct.unpack("<h", rf_data[8:10])[0]) / 100.0 * 1.8 + 32
            ret['Counter'] = struct.unpack('<I', rf_data[4:8])[0]

        elif (status == b'\x1e') or (status == b'\x1f'):
            # Door Sensor
            ret['Type'] = 'Door Sensor'
            if (ord(rf_data[-1]) & 0x01 == 1):
                ret['ReedSwitch']  = 'open'
            else:
                ret['ReedSwitch']  = 'closed'

            if (ord(rf_data[-1]) & 0x02 == 0):
                ret['TamperSwith'] = 'open'
            else:
                ret['TamperSwith'] = 'closed'

            if (status == b'\x1f'):
                ret['Temp_F']      = float(struct.unpack("<h", rf_data[8:10])[0]) / 100.0 * 1.8 + 32
            else:
                ret['Temp_F']      = None

        else:
            logging.error('Unrecognised Device Status')

        return ret

    @staticmethod
    def parse_security_device(rf_data):
        # The switch state is in byte [3] and is a bitfield
        # bit 0 is the magnetic reed switch state
        # bit 3 is the tamper switch state
        ret = {}
        switchState = ord(rf_data[3])
        if (switchState & 0x01):
            ret['ReedSwitch']  = 'open'
        else:
            ret['ReedSwitch']  = 'closed'

        if (switchState & 0x04):
            ret['TamperSwith'] = 'closed'
        else:
            ret['TamperSwith'] = 'open'

        return ret

    @staticmethod
    def parse_tamper(rf_data):
        # Parse Tamper Switch State Change
        if ord(rf_data[3]) == 0x02:
            return 1
        else:
            return 0