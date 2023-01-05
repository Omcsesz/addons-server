import functools
import ipaddress
import operator
from collections import OrderedDict

from rangefilter.filter import DateRangeFilter as DateRangeFilterBase

from django import forms
from django.contrib import admin
from django.contrib.admin.views.main import (
    ChangeList,
    ChangeListSearchForm,
    SEARCH_VAR,
)
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models.constants import LOOKUP_SEP
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext

from olympia.activity.models import IPLog
from olympia.amo.models import GroupConcat, Inet6Ntoa
from .models import FakeEmail


class AMOModelAdminChangeListSearchForm(ChangeListSearchForm):
    def clean(self):
        self.cleaned_data = super().clean()
        search_term = self.cleaned_data[SEARCH_VAR]
        if ',' in search_term:
            self.cleaned_data[SEARCH_VAR] = ','.join(
                term.strip() for term in search_term.split(',') if term.strip()
            )
        return self.cleaned_data


class AMOModelAdminChangeList(ChangeList):
    """Custom ChangeList companion for AMOModelAdmin, allowing to have a custom
    search form and providing support for query string containing the same
    parameter multiple times."""

    search_form_class = AMOModelAdminChangeListSearchForm


class AMOModelAdmin(admin.ModelAdmin):
    class Media:
        js = (
            'js/admin/ip_address_search.js',
            'js/exports.js',
            'js/node_lib/netmask.js',
        )
        css = {'all': ('css/admin/amoadmin.css',)}

    # Classes that want to implement search by ip can override these if needed.
    search_by_ip_actions = ()  # Deactivated by default.
    search_by_ip_activity_accessor = 'activitylog'
    search_by_ip_activity_reverse_accessor = 'activity_log__user'
    # get_search_results() below searches using `IPLog`. It sets an annotation
    # that we can then use in the custom `known_ip_adresses` method referenced
    # in the line below, which is added to the` list_display` fields for IP
    # searches.
    extra_list_display_for_ip_searches = ('known_ip_adresses',)
    # We rarely care about showing this: it's the full count of the number of
    # objects for this model in the database, unfiltered. It does an extra
    # COUNT() query, so avoid it by default.
    show_full_result_count = False

    def get_changelist(self, request, **kwargs):
        return AMOModelAdminChangeList

    def get_search_id_field(self, request):
        """
        Return the field to use when all search terms are numeric.

        Default is to return pk, but in some cases it'll make more sense to
        return a foreign key.
        """
        return 'pk'

    def get_search_query(self, request):
        # We don't have access to the _search_form instance the ChangeList
        # creates, so make our own just for this method to grab the cleaned
        # search term.
        search_form = AMOModelAdminChangeListSearchForm(request.GET)
        return (
            search_form.cleaned_data.get(SEARCH_VAR) if search_form.is_valid() else None
        )

    def get_list_display(self, request):
        """Get fields to use for displaying changelist."""
        list_display = super().get_list_display(request)
        if (
            search_term := self.get_search_query(request)
        ) and self.ip_addresses_and_networks_from_query(search_term):
            return (*list_display, *self.extra_list_display_for_ip_searches)
        return list_display

    def lookup_spawns_duplicates(self, opts, lookup_path):
        """
        Return True if 'distinct()' should be used to query the given lookup
        path. Used by get_search_results() as a replacement of the version used
        by django, which doesn't consider our translation fields as needing
        distinct (but they do).
        """
        # The utility function was admin.utils.lookup_needs_distinct in django3.2;
        # it was renamed to admin.utils.lookup_spawns_duplicates in django4.0
        lookup_function = getattr(
            admin.utils, 'lookup_spawns_duplicates', None
        ) or getattr(admin.utils, 'lookup_needs_distinct')
        rval = lookup_function(opts, lookup_path)
        lookup_fields = lookup_path.split(LOOKUP_SEP)
        # Not pretty but looking up the actual field would require truly
        # resolving the field name, walking to any relations we find up until
        # the last one, that would be a lot of work for a simple edge case.
        if any(
            field_name in lookup_fields
            for field_name in ('localized_string', 'localized_string_clean')
        ):
            rval = True
        return rval

    def ip_addresses_and_networks_from_query(self, search_term):
        # Caller should already have cleaned up search_term at this point,
        # removing whitespace etc if there is a comma separating multiple
        # terms.
        search_terms = search_term.split(',')
        ips = []
        networks = []
        for term in search_terms:
            # If term is a number, skip trying to recognize an IP address
            # entirely, because ip_address() is able to understand IP addresses
            # as integers, and we don't want that, it's likely an user ID.
            if term.isdigit():
                return None
            # Is the search term an IP ?
            try:
                ips.append(ipaddress.ip_address(term))
                continue
            except ValueError:
                pass
            # Is the search term a network ?
            try:
                networks.append(ipaddress.ip_network(term))
                continue
            except ValueError:
                pass
            # Is the search term an IP range ?
            if term.count('-') == 1:
                try:
                    networks.extend(
                        ipaddress.summarize_address_range(
                            *(ipaddress.ip_address(i.strip()) for i in term.split('-'))
                        )
                    )
                    continue
                except (ValueError, TypeError):
                    pass
            # That search term doesn't look like an IP, network or range, so
            # we're not doing an IP search.
            return None
        return {'ips': ips, 'networks': networks}

    def get_queryset_with_related_ips(self, request, queryset, ips_and_networks):
        condition = models.Q()
        if ips_and_networks is not None:
            if ips_and_networks['ips']:
                # IPs search can be implemented in a single __in=() query.
                arg = (
                    f'{self.search_by_ip_activity_accessor}__'
                    'iplog__ip_address_binary__in'
                )
                condition |= models.Q(**{arg: ips_and_networks['ips']})
            if ips_and_networks['networks']:
                # Networks search need one __range conditions for each network.
                arg = (
                    f'{self.search_by_ip_activity_accessor}__'
                    'iplog__ip_address_binary__range'
                )
                for network in ips_and_networks['networks']:
                    condition |= models.Q(**{arg: (network[0], network[-1])})
        annotations = {
            'activity_ips': GroupConcat(
                Inet6Ntoa(
                    f'{self.search_by_ip_activity_accessor}__iplog__ip_address_binary'
                ),
                distinct=True,
            ),
            # Add an annotation for {search_by_ip_activity_accessor}__iplog__id
            # so that we can apply a filter on the specific JOIN that will be
            # used to grab the IPs through GroupConcat to help MySQL optimizer
            # remove non relevant activities from the DISTINCT bit.
            'activity_ips_ids': models.F(
                f'{self.search_by_ip_activity_accessor}__iplog__id'
            ),
        }
        if condition:
            arg = f'{self.search_by_ip_activity_accessor}__action__in'
            condition &= models.Q(**{arg: self.search_by_ip_actions})
            # When searching, we want to duplicate the joins against
            # activitylog + iplog so that one is used for the group concat
            # showing all IPs for activities related to that object and another
            # for the search results. Django doesn't let us do that out of the
            # box, but through FilteredRelation we can force it...
            annotations['activitylog_filtered'] = models.FilteredRelation(
                f'{self.search_by_ip_activity_accessor}__iplog',
                condition=condition,
            )
        queryset = queryset.annotate(**annotations)
        if condition:
            queryset = queryset.filter(
                activity_ips_ids__isnull=False,
                activitylog_filtered__isnull=False,
            )
        # A GROUP_BY will already have been applied thanks to our annotations
        # so we can let django know there won't be any duplicates and avoid
        # doing a DISTINCT.
        may_have_duplicates = False
        return queryset, may_have_duplicates

    def get_search_results(self, request, queryset, search_term):
        """
        Return a tuple containing a queryset to implement the search,
        and a boolean indicating if the results may contain duplicates.

        Originally copied from Django's, but with the following differences:
        - The operator joining the query parts is dynamic: if the search term
          contain a comma and no space, then the comma is used as the separator
          instead, and the query parts are joined by OR, not AND, allowing
          admins to search by a list of ids, emails or usernames and find all
          objects in that list.
        - If the search terms are all numeric and there is more than one, then
          we also restrict the fields we search to the one returned by
          get_search_id_field(request) using a __in ORM lookup directly.
        - If the search terms are all IP addresses, a special search for
          objects matching those IPs is triggered

        """
        # Apply keyword searches.
        def construct_search(field_name):
            if field_name.startswith('^'):
                return '%s__istartswith' % field_name[1:]
            elif field_name.startswith('='):
                return '%s__iexact' % field_name[1:]
            elif field_name.startswith('@'):
                return '%s__icontains' % field_name[1:]
            # Use field_name if it includes a lookup.
            opts = queryset.model._meta
            lookup_fields = field_name.split(models.constants.LOOKUP_SEP)
            # Go through the fields, following all relations.
            prev_field = None
            for path_part in lookup_fields:
                if path_part == 'pk':
                    path_part = opts.pk.name
                try:
                    field = opts.get_field(path_part)
                except FieldDoesNotExist:
                    # Use valid query lookups.
                    if prev_field and prev_field.get_lookup(path_part):
                        return field_name
                else:
                    prev_field = field
                    if hasattr(field, 'get_path_info'):
                        # Update opts to follow the relation.
                        opts = field.get_path_info()[-1].to_opts
            # Otherwise, use the field with icontains.
            return '%s__icontains' % field_name

        if self.search_by_ip_actions:
            ips_and_networks = self.ip_addresses_and_networks_from_query(search_term)
            # If self.search_by_ip_actions is truthy, then we can call
            # get_queryset_with_related_ips(), which will add IP
            # annotations regardless of whether or not we're actually
            # searching by IP...
            queryset, may_have_duplicates = self.get_queryset_with_related_ips(
                request, queryset, ips_and_networks
            )
            # ... We can return here early if we were indeed searching by IP.
            if ips_and_networks:
                return queryset, may_have_duplicates
        else:
            may_have_duplicates = False

        search_fields = self.get_search_fields(request)
        filters = []
        joining_operator = operator.and_
        if not (search_fields and search_term):
            # return early if we have nothing special to do
            return queryset, may_have_duplicates
        # Do our custom logic if a `,` is present. Note that our custom search
        # form (AMOModelAdminChangeListSearchForm) does some preliminary
        # cleaning when it sees a comma, trimming whitespace around each term.
        if ',' in search_term:
            separator = ','
            joining_operator = operator.or_
        else:
            separator = None
        # We support `*` as a wildcard character for our `__like` lookups.
        search_term = search_term.replace('*', '%')
        search_terms = search_term.split(separator)
        if (
            (search_id_field := self.get_search_id_field(request))
            and len(search_terms) >= self.minimum_search_terms_to_search_by_id
            and all(term.isnumeric() for term in search_terms)
        ):
            # if we have at least minimum_search_terms_to_search_by_id terms
            # they are all numeric, we're doing a bulk id search
            queryset = queryset.filter(**{f'{search_id_field}__in': search_terms})
        else:
            orm_lookups = [
                construct_search(str(search_field)) for search_field in search_fields
            ]
            for bit in search_terms:
                or_queries = [
                    models.Q(**{orm_lookup: bit}) for orm_lookup in orm_lookups
                ]

                q_for_this_term = models.Q(functools.reduce(operator.or_, or_queries))
                filters.append(q_for_this_term)

            may_have_duplicates |= any(
                # Use our own lookup_spawns_duplicates(), not django's.
                self.lookup_spawns_duplicates(self.opts, search_spec)
                for search_spec in orm_lookups
            )

            if filters:
                queryset = queryset.filter(functools.reduce(joining_operator, filters))
        return queryset, may_have_duplicates

    def known_ip_adresses(self, obj):
        # activity_ips is an annotation added by get_search_results() above
        # thanks to a GROUP_CONCAT. If present, use that (avoiding making
        # extra queries for each row of results), otherwise, look where
        # appropriate.
        unset = object()
        activity_ips = getattr(obj, 'activity_ips', unset)
        if activity_ips is not unset:
            # The GroupConcat value is a comma seperated string of the ip
            # addresses (already converted to string thanks to INET6_NTOA,
            # except if there was nothing to find, then it would be None)
            ip_addresses = set((activity_ips or '').split(','))
        else:
            arg = self.search_by_ip_activity_reverse_accessor
            ip_addresses = set(
                IPLog.objects.filter(**{arg: obj})
                .values_list('ip_address_binary', flat=True)
                .order_by()
                .distinct()
            )

        contents = format_html_join(
            '', '<li>{}</li>', ((ip,) for ip in sorted(ip_addresses))
        )
        return format_html('<ul>{}</ul>', contents)

    known_ip_adresses.short_description = 'IP addresses'

    # Triggering a search by id only isn't always what the admin wants for an
    # all numeric query, but on the other hand is a nice optimization.
    # The default is 2 so that if there is a field in search_fields for which
    # it makes sense to search using a single numeric term, that still works,
    # the id-only search is only triggered for 2 or more terms. This should be
    # overriden by ModelAdmins where it makes sense to do so.
    minimum_search_terms_to_search_by_id = 2


@admin.register(FakeEmail)
class FakeEmailAdmin(admin.ModelAdmin):
    list_display = (
        'created',
        'message',
    )
    actions = ['delete_selected']
    view_on_site = False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class FakeChoicesMixin:
    def choices(self, changelist):
        """
        Fake choices method (we don't need one, we don't really have choices
        for this filter, it's an input widget) that fetches the params and the
        current values for other filters, so that we can feed that into
        the form that our template displays.
        (We don't control the data passed down to the template, so re-using
        this one is our only option)
        """
        # Grab search query parts and filter query parts as tuples of tuples.
        search_query_parts = (
            (((admin.views.main.SEARCH_VAR, changelist.query),))
            if changelist.query
            else ()
        )
        filters_query_parts = tuple(
            (k, v)
            for k, v in changelist.get_filters_params().items()
            if k not in self.expected_parameters()
        )
        # Assemble them into a `query_parts` property on a unique fake choice.
        all_choice = next(super().choices(changelist))
        all_choice['query_parts'] = search_query_parts + filters_query_parts
        yield all_choice


class HTML5DateInput(forms.DateInput):
    format_key = 'DATE_INPUT_FORMATS'
    input_type = 'date'


class HTML5DateTimeInput(forms.DateInput):
    format_key = 'DATE_INPUT_FORMATS'
    input_type = 'datetime-local'


class DateRangeFilter(FakeChoicesMixin, DateRangeFilterBase):
    """
    Custom rangefilter.filters.DateTimeRangeFilter class that uses HTML5
    widgets and a template without the need for inline CSS/JavaScript.
    Needs FakeChoicesMixin for the fake choices the template will be using (the
    upstream implementation depends on inline JavaScript for this, which we
    want to avoid).
    """

    template = 'admin/amo/date_range_filter.html'
    title = gettext('creation date')
    widget = HTML5DateInput

    def _get_form_fields(self):
        return OrderedDict(
            (
                (
                    self.lookup_kwarg_gte,
                    forms.DateField(
                        label='From',
                        widget=self.widget(),
                        localize=True,
                        required=False,
                    ),
                ),
                (
                    self.lookup_kwarg_lte,
                    forms.DateField(
                        label='To',
                        widget=self.widget(),
                        localize=True,
                        required=False,
                    ),
                ),
            )
        )

    def choices(self, changelist):
        # We want a fake 'All' choice as per FakeChoicesMixin, but as of 0.3.15
        # rangefilter's implementation doesn't bother setting the selected
        # property, and our mixin calls super(), so we have to do it here.
        all_choice = next(super().choices(changelist))
        all_choice['selected'] = not any(self.used_parameters)
        yield all_choice
