from feedly.feeds.aggregated_feed import AggregatedFeed
from feedly.serializers.aggregated_activity_serializer import \
    AggregatedActivitySerializer
from feedly.structures.sorted_set import RedisSortedSetCache
import copy
import datetime
import logging
from feedly.activity import Notification
from feedly.aggregators.base import NotificationAggregator

logger = logging.getLogger(__name__)


class NotificationFeed(AggregatedFeed):
    '''
    Similar to an aggregated feed, but adds
    - denormalized counts
    - pubsub signals
    '''
    max_length = 99
    # key format for storing the sorted set
    key_format = 'notification_feed:1:user:%s'
    # the format we use to denormalize the count
    count_format = 'notification_feed:1:user:%(user_id)s:count'
    # the key used for locking
    lock_format = 'notification_feed:1:user:%s:lock'
    # the format we use to send the pubsub update
    pubsub_format = 'notification_feed:1:user:%(user_id)s:pubsub'

    def __init__(self, user_id, redis=None):
        '''
        User id (the user for which we want to read/write notifications)
        '''
        AggregatedFeed.__init__(self, user_id, redis=redis)
        # location to which we denormalize the count
        self.count_key = self.count_format % self.format_dict
        # set the pubsub key if we're using it
        pubsub_format = getattr(self, 'pubsub_format', None)
        pubsub_key = None
        if pubsub_format:
            pubsub_key = self.pubsub_format % self.format_dict
        self.pubsub_key = pubsub_key

        self.lock_key = self.lock_format % self.format_dict

    def get_aggregator(self):
        '''
        Returns the class used for aggregation
        '''
        aggregator_class = NotificationAggregator
        aggregator = aggregator_class()
        return aggregator

    def get_serializer(self):
        serializer = AggregatedActivitySerializer(Notification)
        return serializer

    def add_many(self, activities):
        with self.redis.lock(self.lock_key, timeout=2):
            current_activities = AggregatedFeed.add_many(self, activities)
            # denormalize the count
            count = self.denormalize_count(current_activities)
            # send a pubsub request
            if self.pubsub_key:
                publish_result = self.redis.publish(self.pubsub_key, count)

            # return the current state of the notification feed
            return current_activities

    def get_denormalized_count(self):
        '''
        Returns the denormalized count stored in self.count_key
        '''
        result = self.redis.get(self.count_key) or 0
        result = int(result)
        return result

    def denormalize_count(self, activities):
        '''
        Denormalize the number of unseen aggregated activities to the key
        defined in self.count_key
        '''
        activities.sort(key=lambda x: x.updated_at, reverse=True)
        current_activities = activities[:self.max_length]
        count = self.count_unseen(current_activities)
        logger.debug('denormalizing count %s', count)
        self.redis.set(self.count_key, count)
        # send a pubsub request
        if self.pubsub_key:
            publish_result = self.redis.publish(self.pubsub_key, count)

        return count

    def count_unseen(self, activities=None):
        '''
        Counts the number of aggregated activities which are unseen
        '''
        count = 0
        if activities is None:
            activities = self[:self.max_length]
        for a in activities:
            if not a.is_seen():
                count += 1
        return count

    def mark_all(self, seen=True, read=None):
        '''
        Mark all the entries as seen or read
        '''
        # TODO refactor this code
        with self.redis.lock(self.lock_key, timeout=2):
            # get the current aggregated activities
            activities = self[:self.max_length]
            # create the update dict
            update_dict = {}

            for activity in activities:
                changed = False
                old_activity = copy.deepcopy(activity)
                if seen is True and not activity.is_seen():
                    activity.seen_at = datetime.datetime.today()
                    changed = True
                if read is True and not activity.is_read():
                    activity.read_at = datetime.datetime.today()
                    changed = True

                if changed:
                    update_dict[old_activity] = activity

            # now add the new ones and remove the old ones in one atomic operation
            to_delete = []
            to_add = []

            for old, new in update_dict.items():
                new_value = self.serialize_activity(new)
                new_score = self.get_activity_score(new)
                to_delete.append(old)

                to_add.append((new_value, new_score))

            # pipeline all our writes to improve performance
            with self.map():
                if to_delete:
                    delete_results = self.remove_many(to_delete)

                # add the data in batch
                if to_add:
                    add_results = RedisSortedSetCache.add_many(self, to_add)

                # denormalize the count
                count = self.denormalize_count(activities)

            # return the new activities
            return activities