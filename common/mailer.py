"""Gmail helpers used by run.py. Not part of this sample; the tests stub them."""


def send(subject, text, html, *args, **kwargs):
    raise NotImplementedError("plug in your own mailer here")


def latest_message(service, thread_id):
    raise NotImplementedError


def label_thread(service, thread_id, add, remove=()):
    raise NotImplementedError
