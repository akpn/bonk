# Copyright 2017 Klarna AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import netaddr
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework import generics
from rest_framework import permissions
from django_rethink import RethinkAPIMixin, RethinkSerializerPermission
from bonk.serializers import *

logger = logging.getLogger("bonk.views")

class IsAdminForUpdate(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return request.user.is_superuser
        else:
            return True

class VRFListView(RethinkAPIMixin, generics.ListCreateAPIView):
    serializer_class = VRFSerializer
    permission_classes = (permissions.IsAuthenticated, IsAdminForUpdate)

class VRFDetailView(RethinkAPIMixin, generics.RetrieveUpdateAPIView):
    serializer_class = VRFSerializer
    permission_classes = (permissions.IsAuthenticated, IsAdminForUpdate)

    def get_slug(self):
        return int(self.kwargs['vrf'])

class IPBlockListView(RethinkAPIMixin, generics.ListCreateAPIView):
    serializer_class = IPBlockSerializer
    group_filter_fields = ['permissions_read', 'permissions_write', 'permissions_create']
    permission_classes = (permissions.IsAuthenticated, RethinkSerializerPermission)

class IPBlockDetailView(RethinkAPIMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = IPBlockSerializer
    group_filter_fields = ['permissions_read', 'permissions_write', 'permissions_create']
    permission_classes = (permissions.IsAuthenticated, RethinkSerializerPermission)

    def get_slug(self):
        return [int(self.kwargs['vrf']), self.kwargs['network'], int(self.kwargs['length'])]

class IPBlockAllocateView(RethinkAPIMixin, generics.CreateAPIView):
    serializer_class = IPBlockSerializer
    group_filter_fields = ['permissions_create']
    permission_classes = (permissions.IsAuthenticated, RethinkSerializerPermission)

    def get_slug(self):
        return [int(self.kwargs['vrf']), self.kwargs['network'], int(self.kwargs['length'])]

    def create(self, *args, **kwargs):
        block = self.get_object()
        if 'hosts' in self.request.data and 'length' not in self.request.data:
            if isinstance(self.request.data['hosts'], int):
                s = bin(self.request.data['hosts'] + 2)[2:]
                length = ((32 - len(s)) * "0" + s).find("1")
            else:
                raise serializers.ValidationError("hosts is invalid")
        elif 'length' not in self.request.data:
            raise serializers.ValidationError("prefix length is required")
        elif (not isinstance(self.request.data['length'], int) or
              self.request.data['length'] < block['length'] or
              self.request.data['length'] > 31):
            raise serializers.ValidationError("prefix length is invalid")
        else:
            length = self.request.data['length']
        if 'permissions' not in self.request.data:
            raise serializers.ValidationError("permissions is required")
        pool = netaddr.IPSet([netaddr.IPNetwork("%s/%d" % (block['network'], block['length']))])
        used = netaddr.IPSet()
        for prefix in IPPrefixSerializer.filter_by_block(block):
            used.add(netaddr.IPNetwork("%s/%d" % (prefix['network'], prefix['length'])))
        available = pool ^ used
        larger = None
        for prefix in available.iter_cidrs():
            if prefix.prefixlen == length:
                break
            elif prefix.prefixlen < length and (larger is None or larger.prefixlen > prefix.prefixlen):
                larger = prefix
        else:
            if larger is None:
                raise serializers.ValidationError("IP block is exhausted")
            prefix = larger.subnet(length).next()
        obj = {
            'vrf': block['vrf'],
            'network': str(prefix.network),
            'length': prefix.prefixlen,
            'state': self.request.data.get('state', 'allocated'),
            'permissions': self.request.data['permissions'],
        }
        if 'reference' in self.request.data:
            obj['reference'] = self.request.data['reference']
        serializer = IPPrefixSerializer(None, data=obj, context={'request': self.request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save(), status=status.HTTP_201_CREATED)

class IPPrefixListView(RethinkAPIMixin, generics.ListCreateAPIView):
    serializer_class = IPPrefixSerializer
    group_filter_fields = ['permissions_read', 'permissions_create', 'permissions_write']
    permission_classes = (permissions.IsAuthenticated, RethinkSerializerPermission)

class IPPrefixDetailView(RethinkAPIMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = IPPrefixSerializer
    group_filter_fields = ['permissions_read', 'permissions_create', 'permissions_write']
    permission_classes = (permissions.IsAuthenticated, RethinkSerializerPermission)

    def get_slug(self):
        return [int(self.kwargs['vrf']), self.kwargs['network'], int(self.kwargs['length'])]

class IPPrefixAllocateView(RethinkAPIMixin, generics.CreateAPIView):
    serializer_class = IPPrefixSerializer
    group_filter_fields = ['permissions_create']
    permission_classes = (permissions.IsAuthenticated, RethinkSerializerPermission)

    def get_slug(self):
        return [int(self.kwargs['vrf']), self.kwargs['network'], int(self.kwargs['length'])]

    def create(self, *args, **kwargs):
        prefix = self.get_object()
        if 'name' not in self.request.data:
            raise serializers.ValidationError("name is required")
        network = netaddr.IPNetwork("%s/%d" % (prefix['network'], prefix['length']))
        used = netaddr.IPSet()
        for address in IPAddressSerializer.filter_by_prefix(prefix):
            used.add(netaddr.IPAddress(address['ip']))
        if prefix['length'] <= 30:
            used.add(network.network)
            used.add(network.broadcast)
        available = netaddr.IPSet(network) ^ used
        if 'ip' not in self.request.data:
            for address in available:
                break
            else:
                raise serializers.ValidationError("network is exhausted")
        else:
            address = netaddr.IPAddress(self.request.data['ip'])
            if address not in available:
                raise serializers.ValidationError("ip='%s' is already in use" % self.request.data['ip'])
        obj = {
            'state': self.request.data.get('state', 'allocated'),
            'vrf': prefix['vrf'],
            'ip': str(address),
            'name': self.request.data['name'],
        }
        for field in ['reference', 'permissions', 'dhcp_mac']:
            if field in self.request.data:
                obj[field] = self.request.data[field]
        serializer = IPAddressSerializer(None, data=obj, context={'request': self.request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save(), status=status.HTTP_201_CREATED)

class HasAddressPermission(RethinkSerializerPermission):
    def has_object_permission(self, request, view, obj):
        if super(HasAddressPermission, self).has_object_permission(request, view, obj):
            return True
        permission = self.get_permission(request, view, obj)
        user_groups = set(request.user.groups.all().values_list('name', flat=True))
        prefix = IPPrefixSerializer.get_by_ip(obj['vrf'], obj['ip'])
        return len(user_groups.intersection(self.get_groups(prefix, permission))) > 0

class IPAddressListView(RethinkAPIMixin, generics.ListCreateAPIView):
    serializer_class = IPAddressSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def default_filter_queryset(self, queryset):
        if self.request.user.is_superuser:
            return queryset
        groups = self.request.user.groups.all().values_list('name', flat=True)
        prefixes = list(
            reduce(lambda x, y: x.union(y),
                [IPPrefixSerializer.filter(reql=True).get_all(*groups, index=i)
                    for i in ['permissions_read', 'permissions_create', 'permissions_write']
                ]
            ).distinct().run(self.get_connection())
        )
        querysets = [IPAddressSerializer.filter_by_prefix(prefix, reql=True)
            for prefix in prefixes
        ] + [IPAddressSerializer.filter(reql=True).get_all(*groups, index=i)
            for i in ['permissions_read', 'permissions_create', 'permissions_write']
        ]
        queryset = reduce(lambda x, y: x.union(y), querysets)
        queryset = queryset.distinct()
        return queryset

class IPAddressDetailView(RethinkAPIMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = IPAddressSerializer
    permission_classes = (permissions.IsAuthenticated, HasAddressPermission)

    def get_slug(self):
        return [int(self.kwargs['vrf']), self.kwargs['ip']]

class DNSZoneListView(RethinkAPIMixin, generics.ListCreateAPIView):
    serializer_class = DNSZoneSerializer
    group_filter_fields = ['permissions_read', 'permissions_create', 'permissions_write']
    permission_classes = (permissions.IsAuthenticated,)

class DNSZoneDetailView(RethinkAPIMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = DNSZoneSerializer
    group_filter_fields = ['permissions_read', 'permissions_create', 'permissions_write']
    permission_classes = (permissions.IsAuthenticated, RethinkSerializerPermission)

class DNSRecordListView(RethinkAPIMixin, generics.ListCreateAPIView):
    serializer_class = DNSRecordSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def default_filter_queryset(self, queryset):
        if self.request.user.is_superuser:
            return queryset
        queryset = queryset. \
            merge(lambda record: {"zone_obj":
                DNSZoneSerializer.filter(name=record['zone'], reql=True).nth(0)
            })
        groups = self.request.user.groups.all().values_list('name', flat=True)
        queryset = queryset.filter(lambda record:
            record['zone_obj']['permissions']['read'].default([]).set_union(
                record['zone_obj']['permissions']['write'].default([])
            ).set_union(
                record['permissions']['read'].default([])
            ).set_union(
                record['permissions']['write'].default([])
            ).set_intersection(groups).count() > 0)
        return queryset

class HasRecordPermission(RethinkSerializerPermission):
    def has_object_permission(self, request, view, obj):
        if super(HasRecordPermission, self).has_object_permission(request, view, obj):
            return True
        permission = self.get_permission(request, view, obj)
        user_groups = set(request.user.groups.all().values_list('name', flat=True))
        zone = DNSZoneSerializer.get(name=obj['zone'])
        return len(user_groups.intersection(self.get_groups(zone, permission))) > 0

class DNSRecordDetailView(RethinkAPIMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = DNSRecordSerializer
    permission_classes = (permissions.IsAuthenticated, HasRecordPermission)

    def get_slug(self):
        return [self.kwargs.get('name'), self.kwargs.get('type')]
