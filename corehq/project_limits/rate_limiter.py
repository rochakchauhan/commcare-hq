import random
import time

import attr

from corehq.apps.accounting.models import Subscription
from corehq.apps.users.models import CommCareUser
from corehq.project_limits.rate_counter.presets import (
    day_rate_counter,
    hour_rate_counter,
    minute_rate_counter,
    second_rate_counter,
    week_rate_counter,
)
from corehq.util.quickcache import quickcache


class RateLimiter(object):
    """
    Example usage:

    >>> per_user_rate_def = RateDefinition(per_week=50000, per_day=13000, per_second=.001)
    >>> min_rate_def = RateDefinition(per_second=10)
    >>> per_user_rate_def = PerUserRateDefinition(per_user_rate_def, min_rate_def)
    >>> my_feature_rate_limiter = RateLimiter('my_feature', per_user_rate_def.get_rate_limits)
    >>> if my_feature_rate_limiter.allow_usage('my_domain'):
    ...     # ...do stuff...
    ...     my_feature_rate_limiter.report_usage('my_domain')

    """
    def __init__(self, feature_key, get_rate_limits):
        self.feature_key = feature_key
        self.get_rate_limits = get_rate_limits

    def get_normalized_scope(self, scope):
        if isinstance(scope, str):
            scope = (scope,)
        elif not isinstance(scope, tuple):
            raise ValueError("scope must be a string or tuple: {!r}".format(scope))

        return scope

    def report_usage(self, scope, delta=1):
        scope = self.get_normalized_scope(scope)
        for rate_counter, limit in self.get_rate_limits(*scope):
            rate_counter.increment((self.feature_key,) + scope, delta=delta)

    def allow_usage(self, scope):
        return all(current_rate < limit
                   for rate_counter_key, current_rate, limit in self.iter_rates(scope))

    def iter_rates(self, scope):
        """
        Get generator of (key, current rate, rate limit) as applies to scope

        e.g.
            ('week', 92359, 115000)
            ('day', ...)
            ...

        """
        scope = self.get_normalized_scope(scope)
        return (
            (rate_counter.key, rate_counter.get((self.feature_key,) + scope), limit)
            for rate_counter, limit in self.get_rate_limits(*scope)
        )

    def wait(self, scope, timeout):
        start = time.time()
        target_end = start + timeout
        delay = 0
        while True:
            if self.allow_usage(scope):
                return True
            # add a random amount between 100ms and 500ms to the last delay
            # so that the delays get a bit longer each time
            # and different requests don't have exactly the same schedule
            delay += random.uniform(0.1, 0.5)
            # if the delay would bring us past the timeout
            # rescheduled for right at the timeout instead
            delay = min(delay, target_end - time.time())
            if delay < 0.01:
                return False
            else:
                time.sleep(delay)


@quickcache(['domain'], memoize_timeout=60, timeout=60 * 60)
def get_n_users_for_rate_limiting(domain):
    """
    Returns the number of users "allocated" to the project

    That is, the actual number of users or the number of users included in the subscription,
    whichever is higher.

    This number is then used to portion out resource allocation through rate limiting.

    """
    n_users = _get_user_count(domain)
    n_users_included_in_subscription = _get_users_included_in_subscription(domain)
    return max(n_users_included_in_subscription, n_users)


def _get_user_count(domain):
    return CommCareUser.total_by_domain(domain, is_active=True)


def _get_users_included_in_subscription(domain):
    subscription = Subscription.get_active_subscription_by_domain(domain)
    if subscription:
        plan_version = subscription.plan_version

        n_included_users = (
            plan_version.feature_rates.get(feature__feature_type='User').monthly_limit)

        if plan_version.plan.is_customer_software_plan:
            n_domains = len(plan_version.subscription_set.filter(is_active=True))
            return n_included_users / n_domains
        else:
            return n_included_users
    else:
        return 0


class PerUserRateDefinition(object):
    def __init__(self, per_user_rate_definition, constant_rate_definition=None):
        self.per_user_rate_definition = per_user_rate_definition
        self.constant_rate_definition = constant_rate_definition or RateDefinition()

    def get_rate_limits(self, domain):
        n_users = get_n_users_for_rate_limiting(domain)
        return (
            self.per_user_rate_definition
            .times(n_users)
            .plus(self.constant_rate_definition)
        ).rate_limits


@attr.s
class RateDefinition(object):
    per_week = attr.ib(default=None)
    per_day = attr.ib(default=None)
    per_hour = attr.ib(default=None)
    per_minute = attr.ib(default=None)
    per_second = attr.ib(default=None)

    def times(self, multiplier):
        return self.map(lambda value: value * multiplier if value is not None else value)

    def plus(self, other):
        return self.map(lambda value, other_value:
                        None if value is None else value + (other_value or 0), other)

    def map(self, math_func, other=None):
        """
        Create new rate definition object with math_func applied to each value of self

        This is essentially RateDefinition(per_week=math_func(self.per_week), ...)
        but deals better with None values and uses meta-programming to avoid copy-paste errors.
        """
        kwargs = {}
        for attribute in self.__attrs_attrs__:
            value = getattr(self, attribute.name)
            if other:
                other_value = getattr(other, attribute.name)
                kwargs[attribute.name] = math_func(value, other_value)
            else:
                kwargs[attribute.name] = math_func(value)
        return self.__class__(**kwargs)

    @property
    def rate_limits(self):
        return [(rate_counter, limit) for limit, rate_counter in (
            (self.per_week, week_rate_counter),
            (self.per_day, day_rate_counter),
            (self.per_hour, hour_rate_counter),
            (self.per_minute, minute_rate_counter),
            (self.per_second, second_rate_counter),
        ) if limit]
