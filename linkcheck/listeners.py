import os.path
import sys
import time
from threading import Thread

try:
    import Queue
except ImportError:
    # Python 3
    import queue as Queue

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.db.models import signals as model_signals

try:
    from filebrowser.views import filebrowser_post_upload
    from filebrowser.views import filebrowser_post_rename
    from filebrowser.views import filebrowser_post_delete
    from filebrowser.settings import DIRECTORY
    FILEBROWSER_PRESENT = True
except ImportError:
    FILEBROWSER_PRESENT = False

from . import update_lock
from linkcheck.models import Url, Link


tasks_queue = Queue.LifoQueue()
worker_running = False
tests_running = len(sys.argv) > 1 and sys.argv[1] == 'test' or sys.argv[0].endswith('runtests.py')


def linkcheck_worker():
    global worker_running
    while tasks_queue.not_empty:
        task = tasks_queue.get()
        task['target'](*task['args'], **task['kwargs'])
        tasks_queue.task_done()
    worker_running = False


def start_worker():
    global worker_running
    if worker_running is False:
        worker_running = True
        t = Thread(target=linkcheck_worker)
        t.daemon = True
        t.start()


def check_instance_links(sender, instance, **kwargs):
    """
    When an object is saved:
        new Link/Urls are created, checked

    When an object is modified:
        new link/urls are created, checked
        existing link/urls are checked
        Removed links are deleted
    """
    linklist_cls = sender._linklist

    def do_check_instance_links(sender, instance, wait=False):
        # On some installations, this wait time might be enough for the
        # thread transaction to account for the object change (GH #41).
        # A candidate for the future post_commit signal.

        global worker_running

        if wait:
            time.sleep(0.1)
        with update_lock:
            content_type = linklist_cls.content_type()
            new_links = []
            old_links = Link.objects.filter(content_type=content_type, object_id=instance.pk)

            linklists = linklist_cls().get_linklist(extra_filter={'pk':instance.pk,})

            if not linklists:
                # This object is no longer watched by linkcheck according to object_filter
                links = []
            else:
                linklist = linklists[0]
                links = linklist['urls']+linklist['images']

            for link in links:
                # url structure = (field, link text, url)
                url = link[2]
                internal_hash = False
                if url.startswith('#'):
                    internal_hash = url
                    url = instance.get_absolute_url() + url
                u, created = Url.objects.get_or_create(url=url)
                l, created = Link.objects.get_or_create(url=u, field=link[0], text=link[1], content_type=content_type, object_id=instance.pk)
                new_links.append(l.id)
                u.still_exists = True
                if internal_hash:
                    setattr(u, '_internal_hash', internal_hash)
                    setattr(u, '_instance', instance)
                u.check_url()

            gone_links = old_links.exclude(id__in=new_links)
            gone_links.delete()

    # Don't run in a separate thread if we are running tests
    if tests_running:
        do_check_instance_links(sender, instance)
    else:
        tasks_queue.put({
            'target': do_check_instance_links,
            'args': (sender, instance, True),
            'kwargs': {}
        })
        start_worker()


def delete_instance_links(sender, instance, **kwargs):
    """
    Delete all links belonging to a model instance when that instance is deleted
    """
    linklist_cls = sender._linklist
    content_type = linklist_cls.content_type()
    old_links = Link.objects.filter(content_type=content_type, object_id=instance.pk)
    old_links.delete()


def instance_pre_save(sender, instance, **kwargs):
    if not instance.pk:
        # Ignore unsaved instances
        return
    current_url = instance.get_absolute_url()
    previous_url = sender.objects.get(pk=instance.pk).get_absolute_url()
    setattr(instance, '__previous_url', previous_url)
    if previous_url == current_url:
        return
    else:
        if previous_url is not None:
            old_urls = Url.objects.filter(url__startswith=previous_url)
            old_urls.update(status=False, message='Broken internal link')
        if current_url is not None:
            new_urls = Url.objects.filter(url__startswith=current_url)
            # Mark these urls' status as False, so that post_save will check them
            new_urls.update(status=False, message='Should be checked now!')


def instance_post_save(sender, instance, **kwargs):
    def do_instance_post_save(sender, instance, **kwargs):
        current_url = instance.get_absolute_url()
        previous_url = getattr(instance, '__previous_url', None)
        # We assume returning None from get_absolute_url means that this instance doesn't have a URL
        # Not sure if we should do the same for '' as this could refer to '/'
        if current_url is not None and current_url != previous_url:
            linklist_cls = sender._linklist
            active = linklist_cls.objects().filter(pk=instance.pk).count()

            if kwargs['created'] or (not active):
                new_urls = Url.objects.filter(url__startswith=current_url)
            else:
                new_urls = Url.objects.filter(status=False).filter(url__startswith=current_url)

            if new_urls:
                for url in new_urls:
                    url.check_url()

    if tests_running:
        do_instance_post_save(sender, instance, **kwargs)
    else:
        tasks_queue.put({
            'target': do_instance_post_save,
            'args': (sender, instance),
            'kwargs': kwargs
        })
        start_worker()


def instance_pre_delete(sender, instance, **kwargs):
    instance.linkcheck_deleting = True
    deleted_url = instance.get_absolute_url()
    if deleted_url:
        old_urls = Url.objects.filter(url__startswith=deleted_url).exclude(status=False)
        if old_urls:
            old_urls.update(status=False, message='Broken internal link')


# 1. register listeners for the objects that contain Links
for linklist_name, linklist_cls in apps.get_app_config('linkcheck').all_linklists.items():
    model_signals.post_save.connect(check_instance_links, sender=linklist_cls.model)
    model_signals.post_delete.connect(delete_instance_links, sender=linklist_cls.model)

    # 2. register listeners for the objects that are targets of Links,
    # only when get_absolute_url() is defined for the model

    if getattr(linklist_cls.model, 'get_absolute_url', None):
        model_signals.pre_save.connect(instance_pre_save, sender=linklist_cls.model)
        model_signals.post_save.connect(instance_post_save, sender=linklist_cls.model)
        model_signals.pre_delete.connect(instance_pre_delete, sender=linklist_cls.model)


# Integrate with django-filebrowser if present

def get_relative_media_url():
    if settings.MEDIA_URL.startswith('http'):
        relative_media_url = ('/'+'/'.join(settings.MEDIA_URL.split('/')[3:]))[:-1]
    else:
        relative_media_url = settings.MEDIA_URL
    return relative_media_url


def handle_upload(sender, path=None, **kwargs):
    url = os.path.join(get_relative_media_url(), kwargs['file'].url_relative)
    url_qs = Url.objects.filter(url=url).filter(status=False)
    count = url_qs.count()
    if count:
        url_qs.update(status=True, message='Working document link')
        msg = "Please note. Uploading %s has corrected %s broken link%s. See the Link Manager for more details" % (url, count, count>1 and 's' or '')
        messages.info(sender, msg)


def handle_rename(sender, path=None, **kwargs):

    def isdir(filename):
        if filename.count('.'):
            return False
        else:
            return True

    old_url = os.path.join(get_relative_media_url(), DIRECTORY, path, kwargs['filename'])
    new_url = os.path.join(get_relative_media_url(), DIRECTORY, path, kwargs['new_filename'])
    # Renaming a file will cause it's urls to become invalid
    # Renaming a directory will cause the urls of all it's contents to become invalid
    old_url_qs = Url.objects.filter(url=old_url).filter(status=True)
    if isdir(kwargs['filename']):
        old_url_qs = Url.objects.filter(url__startswith=old_url).filter(status=True)
    old_count = old_url_qs.count()
    if old_count:
        old_url_qs.update(status=False, message='Missing Document')
        msg = "Warning. Renaming %s has caused %s link%s to break. Please use the Link Manager to fix them" % (old_url, old_count, old_count>1 and 's' or '')
        messages.info(sender, msg)

    # The new directory may fix some invalid links, so we also check for that
    if isdir(kwargs['new_filename']):
        new_count = 0
        new_url_qs = Url.objects.filter(url__startswith=new_url).filter(status=False)
        for url in new_url_qs:
            if url.check_url():
                new_count += 1
    else:
        new_url_qs = Url.objects.filter(url=new_url).filter(status=False)
        new_count = new_url_qs.count()
        if new_count:
            new_url_qs.update(status=True, message='Working document link')
    if new_count:
        msg = "Please note. Renaming %s has corrected %s broken link%s. See the Link Manager for more details" % (new_url, new_count, new_count>1 and 's' or '')
        messages.info(sender, msg)


def handle_delete(sender, path=None, **kwargs):

    url = os.path.join(get_relative_media_url(), DIRECTORY, path, kwargs['filename'])
    url_qs = Url.objects.filter(url=url).filter(status=True)
    count = url_qs.count()
    if count:
        url_qs.update(status=False, message='Missing Document')
        msg = "Warning. Deleting %s has caused %s link%s to break. Please use the Link Manager to fix them" % (url, count, count>1 and 's' or '')
        messages.info(sender, msg)


if FILEBROWSER_PRESENT:
    filebrowser_post_upload.connect(handle_upload)
    filebrowser_post_rename.connect(handle_rename)
    filebrowser_post_delete.connect(handle_delete)
