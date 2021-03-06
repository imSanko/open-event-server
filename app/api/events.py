from datetime import datetime

import pytz
from flask import request
from flask_jwt_extended import current_user, get_jwt_identity, verify_jwt_in_request
from flask_rest_jsonapi import ResourceDetail, ResourceList, ResourceRelationship
from flask_rest_jsonapi.exceptions import ObjectNotFound
from marshmallow_jsonapi import fields
from marshmallow_jsonapi.flask import Schema
from sqlalchemy import and_, or_
from sqlalchemy.orm.exc import NoResultFound

from app.api.bootstrap import api
from app.api.data_layers.EventCopyLayer import EventCopyLayer
from app.api.helpers.db import safe_query, safe_query_kwargs, save_to_db
from app.api.helpers.errors import ConflictError, ForbiddenError, UnprocessableEntityError
from app.api.helpers.events import create_custom_forms_for_attendees
from app.api.helpers.export_helpers import create_export_job
from app.api.helpers.permission_manager import has_access, is_logged_in
from app.api.helpers.utilities import dasherize
from app.api.schema.events import EventSchema, EventSchemaPublic

# models
from app.models import db
from app.models.access_code import AccessCode
from app.models.custom_form import CustomForms
from app.models.discount_code import DiscountCode
from app.models.email_notification import EmailNotification
from app.models.event import Event
from app.models.event_copyright import EventCopyright
from app.models.event_invoice import EventInvoice
from app.models.faq import Faq
from app.models.faq_type import FaqType
from app.models.feedback import Feedback
from app.models.microlocation import Microlocation
from app.models.order import Order
from app.models.role import Role
from app.models.role_invite import RoleInvite
from app.models.session import Session
from app.models.session_type import SessionType
from app.models.social_link import SocialLink
from app.models.speaker import Speaker
from app.models.speakers_call import SpeakersCall
from app.models.sponsor import Sponsor
from app.models.stripe_authorization import StripeAuthorization
from app.models.tax import Tax
from app.models.ticket import Ticket, TicketTag
from app.models.ticket_holder import TicketHolder
from app.models.track import Track
from app.models.user import (
    ATTENDEE,
    COORGANIZER,
    MARKETER,
    MODERATOR,
    ORGANIZER,
    OWNER,
    REGISTRAR,
    SALES_ADMIN,
    TRACK_ORGANIZER,
    User,
)
from app.models.user_favourite_event import UserFavouriteEvent
from app.models.users_events_role import UsersEventsRoles


def validate_event(user, data):
    if not user.can_create_event():
        raise ForbiddenError({'source': ''}, "Please verify your Email")

    if data.get('state', None) == 'published' and not user.can_publish_event():
        raise ForbiddenError({'source': ''}, "Only verified accounts can publish events")

    if not data.get('name', None) and data.get('state', None) == 'published':
        raise ConflictError(
            {'pointer': '/data/attributes/name'},
            "Event Name is required to publish the event",
        )


def validate_date(event, data):
    if event:
        if 'starts_at' not in data:
            data['starts_at'] = event.starts_at

        if 'ends_at' not in data:
            data['ends_at'] = event.ends_at

    if not data.get('starts_at') or not data.get('ends_at'):
        raise UnprocessableEntityError(
            {'pointer': '/data/attributes/date'},
            "enter required fields starts-at/ends-at",
        )

    if data['starts_at'] >= data['ends_at']:
        raise UnprocessableEntityError(
            {'pointer': '/data/attributes/ends-at'}, "ends-at should be after starts-at"
        )

    if datetime.timestamp(data['starts_at']) <= datetime.timestamp(datetime.now()):
        if event and event.deleted_at and not data.get('deleted_at'):
            data['state'] = 'draft'
        elif event and not event.deleted_at and data.get('deleted_at'):
            pass
        else:
            raise UnprocessableEntityError(
                {'pointer': '/data/attributes/starts-at'},
                "starts-at should be after current date-time",
            )


class EventList(ResourceList):
    def before_get(self, args, kwargs):
        """
        method for assigning schema based on admin access
        :param args:
        :param kwargs:
        :return:
        """
        if is_logged_in() and (has_access('is_admin') or kwargs.get('user_id')):
            self.schema = EventSchema
        else:
            self.schema = EventSchemaPublic

    def query(self, view_kwargs):
        """
        query method for EventList class
        :param view_kwargs:
        :return:
        """
        query_ = self.session.query(Event)
        if get_jwt_identity() is None or not current_user.is_staff:
            # If user is not admin, we only show published events
            query_ = query_.filter_by(state='published')
        if is_logged_in():
            # For a specific user accessing the API, we show all
            # events managed by them, even if they're not published
            verify_jwt_in_request()
            query2 = self.session.query(Event)
            query2 = (
                query2.join(Event.roles)
                .filter_by(user_id=current_user.id)
                .join(UsersEventsRoles.role)
                .filter(
                    or_(
                        Role.name == COORGANIZER,
                        Role.name == ORGANIZER,
                        Role.name == OWNER,
                    )
                )
            )
            query_ = query_.union(query2)

        if view_kwargs.get('user_id') and 'GET' in request.method:
            if not has_access('is_user_itself', user_id=int(view_kwargs['user_id'])):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query_kwargs(User, view_kwargs, 'user_id')
            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name != ATTENDEE)
            )

        if view_kwargs.get('user_owner_id') and 'GET' in request.method:
            if not has_access(
                'is_user_itself', user_id=int(view_kwargs['user_owner_id'])
            ):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query_kwargs(User, view_kwargs, 'user_owner_id')
            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name == OWNER)
            )

        if view_kwargs.get('user_organizer_id') and 'GET' in request.method:
            if not has_access(
                'is_user_itself', user_id=int(view_kwargs['user_organizer_id'])
            ):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query_kwargs(User, view_kwargs, 'user_organizer_id')

            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name == ORGANIZER)
            )

        if view_kwargs.get('user_coorganizer_id') and 'GET' in request.method:
            if not has_access(
                'is_user_itself', user_id=int(view_kwargs['user_coorganizer_id'])
            ):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query_kwargs(User, view_kwargs, 'user_coorganizer_id')
            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name == COORGANIZER)
            )

        if view_kwargs.get('user_track_organizer_id') and 'GET' in request.method:
            if not has_access(
                'is_user_itself', user_id=int(view_kwargs['user_track_organizer_id'])
            ):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query(
                User,
                'id',
                view_kwargs['user_track_organizer_id'],
                'user_organizer_id',
            )
            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name == TRACK_ORGANIZER)
            )

        if view_kwargs.get('user_registrar_id') and 'GET' in request.method:
            if not has_access(
                'is_user_itself', user_id=int(view_kwargs['user_registrar_id'])
            ):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query_kwargs(User, view_kwargs, 'user_registrar_id')
            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name == REGISTRAR)
            )

        if view_kwargs.get('user_moderator_id') and 'GET' in request.method:
            if not has_access(
                'is_user_itself', user_id=int(view_kwargs['user_moderator_id'])
            ):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query_kwargs(User, view_kwargs, 'user_moderator_id')
            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name == MODERATOR)
            )

        if view_kwargs.get('user_marketer_id') and 'GET' in request.method:
            if not has_access(
                'is_user_itself', user_id=int(view_kwargs['user_marketer_id'])
            ):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query_kwargs(User, view_kwargs, 'user_marketer_id')
            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name == MARKETER)
            )

        if view_kwargs.get('user_sales_admin_id') and 'GET' in request.method:
            if not has_access(
                'is_user_itself', user_id=int(view_kwargs['user_sales_admin_id'])
            ):
                raise ForbiddenError({'source': ''}, 'Access Forbidden')
            user = safe_query_kwargs(User, view_kwargs, 'user_sales_admin_id')
            query_ = (
                query_.join(Event.roles)
                .filter_by(user_id=user.id)
                .join(UsersEventsRoles.role)
                .filter(Role.name == SALES_ADMIN)
            )

        if view_kwargs.get('event_type_id') and 'GET' in request.method:
            query_ = self.session.query(Event).filter(
                getattr(Event, 'event_type_id') == view_kwargs['event_type_id']
            )

        if view_kwargs.get('event_topic_id') and 'GET' in request.method:
            query_ = self.session.query(Event).filter(
                getattr(Event, 'event_topic_id') == view_kwargs['event_topic_id']
            )

        if view_kwargs.get('event_sub_topic_id') and 'GET' in request.method:
            query_ = self.session.query(Event).filter(
                getattr(Event, 'event_sub_topic_id') == view_kwargs['event_sub_topic_id']
            )

        if view_kwargs.get('discount_code_id') and 'GET' in request.method:
            event_id = get_id(view_kwargs)['id']
            if not has_access('is_coorganizer', event_id=event_id):
                raise ForbiddenError({'source': ''}, 'Coorganizer access is required')
            query_ = self.session.query(Event).filter(
                getattr(Event, 'discount_code_id') == view_kwargs['discount_code_id']
            )

        return query_

    def before_post(self, args, kwargs, data=None):
        """
        before post method to verify if the event location is provided before publishing the event
        and checks that the user is verified
        :param args:
        :param kwargs:
        :param data:
        :return:
        """
        user = User.query.filter_by(id=kwargs['user_id']).first()
        validate_event(user, data)
        if data['state'] != 'draft':
            validate_date(None, data)

    def after_create_object(self, event, data, view_kwargs):
        """
        after create method to save roles for users and add the user as an accepted role(owner and organizer)
        :param event:
        :param data:
        :param view_kwargs:
        :return:
        """
        user = User.query.filter_by(id=view_kwargs['user_id']).first()
        role = Role.query.filter_by(name=OWNER).first()
        uer = UsersEventsRoles(user=user, event=event, role=role)
        save_to_db(uer, 'Event Saved')
        role_invite = RoleInvite(
            email=user.email,
            role_name=role.title_name,
            event=event,
            role=role,
            status='accepted',
        )
        save_to_db(role_invite, 'Owner Role Invite Added')

        # create custom forms for compulsory fields of attendee form.
        create_custom_forms_for_attendees(event)

        if event.state == 'published' and event.schedule_published_on:
            start_export_tasks(event)

        if data.get('original_image_url'):
            start_image_resizing_tasks(event, data['original_image_url'])

    # This permission decorator ensures, you are logged in to create an event
    # and have filter ?withRole to get events associated with logged in user
    decorators = (
        api.has_permission(
            'create_event',
        ),
    )
    schema = EventSchema
    data_layer = {
        'session': db.session,
        'model': Event,
        'methods': {'after_create_object': after_create_object, 'query': query},
    }


def get_id(view_kwargs):
    """
    method to get the resource id for fetching details
    :param view_kwargs:
    :return:
    """
    if view_kwargs.get('identifier'):
        event = safe_query_kwargs(Event, view_kwargs, 'identifier', 'identifier')
        view_kwargs['id'] = event.id

    if view_kwargs.get('sponsor_id') is not None:
        sponsor = safe_query_kwargs(Sponsor, view_kwargs, 'sponsor_id')
        if sponsor.event_id is not None:
            view_kwargs['id'] = sponsor.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('user_favourite_event_id') is not None:
        user_favourite_event = safe_query_kwargs(
            UserFavouriteEvent,
            view_kwargs,
            'user_favourite_event_id',
        )
        if user_favourite_event.event_id is not None:
            view_kwargs['id'] = user_favourite_event.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('copyright_id') is not None:
        copyright = safe_query_kwargs(EventCopyright, view_kwargs, 'copyright_id')
        if copyright.event_id is not None:
            view_kwargs['id'] = copyright.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('track_id') is not None:
        track = safe_query_kwargs(Track, view_kwargs, 'track_id')
        if track.event_id is not None:
            view_kwargs['id'] = track.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('session_type_id') is not None:
        session_type = safe_query_kwargs(SessionType, view_kwargs, 'session_type_id')
        if session_type.event_id is not None:
            view_kwargs['id'] = session_type.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('faq_type_id') is not None:
        faq_type = safe_query_kwargs(FaqType, view_kwargs, 'faq_type_id')
        if faq_type.event_id is not None:
            view_kwargs['id'] = faq_type.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('event_invoice_id') is not None:
        event_invoice = safe_query_kwargs(EventInvoice, view_kwargs, 'event_invoice_id')
        if event_invoice.event_id is not None:
            view_kwargs['id'] = event_invoice.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('event_invoice_identifier') is not None:
        event_invoice = safe_query_kwargs(
            EventInvoice, view_kwargs, 'event_invoice_identifier', 'identifier'
        )
        if event_invoice.event_id is not None:
            view_kwargs['id'] = event_invoice.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('discount_code_id') is not None:
        discount_code = safe_query_kwargs(DiscountCode, view_kwargs, 'discount_code_id')
        if discount_code.event_id is not None:
            view_kwargs['id'] = discount_code.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('session_id') is not None:
        sessions = safe_query_kwargs(Session, view_kwargs, 'session_id')
        if sessions.event_id is not None:
            view_kwargs['id'] = sessions.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('social_link_id') is not None:
        social_link = safe_query_kwargs(SocialLink, view_kwargs, 'social_link_id')
        if social_link.event_id is not None:
            view_kwargs['id'] = social_link.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('tax_id') is not None:
        tax = safe_query_kwargs(Tax, view_kwargs, 'tax_id')
        if tax.event_id is not None:
            view_kwargs['id'] = tax.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('stripe_authorization_id') is not None:
        stripe_authorization = safe_query_kwargs(
            StripeAuthorization,
            view_kwargs,
            'stripe_authorization_id',
        )
        if stripe_authorization.event_id is not None:
            view_kwargs['id'] = stripe_authorization.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('user_id') is not None:
        try:
            discount_code = (
                db.session.query(DiscountCode)
                .filter_by(id=view_kwargs['discount_code_id'])
                .one()
            )
        except NoResultFound:
            raise ObjectNotFound(
                {'parameter': 'discount_code_id'},
                "DiscountCode: {} not found".format(view_kwargs['discount_code_id']),
            )
        else:
            if discount_code.event_id is not None:
                view_kwargs['id'] = discount_code.event_id
            else:
                view_kwargs['id'] = None

    if view_kwargs.get('speakers_call_id') is not None:
        speakers_call = safe_query_kwargs(SpeakersCall, view_kwargs, 'speakers_call_id')
        if speakers_call.event_id is not None:
            view_kwargs['id'] = speakers_call.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('ticket_id') is not None:
        ticket = safe_query_kwargs(Ticket, view_kwargs, 'ticket_id')
        if ticket.event_id is not None:
            view_kwargs['id'] = ticket.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('ticket_tag_id') is not None:
        ticket_tag = safe_query_kwargs(TicketTag, view_kwargs, 'ticket_tag_id')
        if ticket_tag.event_id is not None:
            view_kwargs['id'] = ticket_tag.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('role_invite_id') is not None:
        role_invite = safe_query_kwargs(RoleInvite, view_kwargs, 'role_invite_id')
        if role_invite.event_id is not None:
            view_kwargs['id'] = role_invite.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('users_events_role_id') is not None:
        users_events_role = safe_query_kwargs(
            UsersEventsRoles,
            view_kwargs,
            'users_events_role_id',
        )
        if users_events_role.event_id is not None:
            view_kwargs['id'] = users_events_role.event_id

    if view_kwargs.get('access_code_id') is not None:
        access_code = safe_query_kwargs(AccessCode, view_kwargs, 'access_code_id')
        if access_code.event_id is not None:
            view_kwargs['id'] = access_code.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('speaker_id'):
        try:
            speaker = (
                db.session.query(Speaker).filter_by(id=view_kwargs['speaker_id']).one()
            )
        except NoResultFound:
            raise ObjectNotFound(
                {'parameter': 'speaker_id'},
                "Speaker: {} not found".format(view_kwargs['speaker_id']),
            )
        else:
            if speaker.event_id:
                view_kwargs['id'] = speaker.event_id
            else:
                view_kwargs['id'] = None

    if view_kwargs.get('email_notification_id'):
        try:
            email_notification = (
                db.session.query(EmailNotification)
                .filter_by(id=view_kwargs['email_notification_id'])
                .one()
            )
        except NoResultFound:
            raise ObjectNotFound(
                {'parameter': 'email_notification_id'},
                "Email Notification: {} not found".format(
                    view_kwargs['email_notification_id']
                ),
            )
        else:
            if email_notification.event_id:
                view_kwargs['id'] = email_notification.event_id
            else:
                view_kwargs['id'] = None

    if view_kwargs.get('microlocation_id'):
        try:
            microlocation = (
                db.session.query(Microlocation)
                .filter_by(id=view_kwargs['microlocation_id'])
                .one()
            )
        except NoResultFound:
            raise ObjectNotFound(
                {'parameter': 'microlocation_id'},
                "Microlocation: {} not found".format(view_kwargs['microlocation_id']),
            )
        else:
            if microlocation.event_id:
                view_kwargs['id'] = microlocation.event_id
            else:
                view_kwargs['id'] = None

    if view_kwargs.get('attendee_id'):
        attendee = safe_query_kwargs(TicketHolder, view_kwargs, 'attendee_id')
        if attendee.event_id is not None:
            view_kwargs['id'] = attendee.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('custom_form_id') is not None:
        custom_form = safe_query_kwargs(CustomForms, view_kwargs, 'custom_form_id')
        if custom_form.event_id is not None:
            view_kwargs['id'] = custom_form.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('faq_id') is not None:
        faq = safe_query_kwargs(Faq, view_kwargs, 'faq_id')
        if faq.event_id is not None:
            view_kwargs['id'] = faq.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('order_identifier') is not None:
        order = safe_query_kwargs(Order, view_kwargs, 'order_identifier', 'identifier')
        if order.event_id is not None:
            view_kwargs['id'] = order.event_id
        else:
            view_kwargs['id'] = None

    if view_kwargs.get('feedback_id') is not None:
        feedback = safe_query_kwargs(Feedback, view_kwargs, 'feedback_id')
        if feedback.event_id is not None:
            view_kwargs['id'] = feedback.event_id
        else:
            view_kwargs['id'] = None

    return view_kwargs


class EventDetail(ResourceDetail):
    """
    EventDetail class for EventSchema
    """

    def before_get(self, args, kwargs):
        """
        method for assigning schema based on access
        :param args:
        :param kwargs:
        :return:
        """
        kwargs = get_id(kwargs)
        if is_logged_in() and has_access('is_coorganizer', event_id=kwargs['id']):
            self.schema = EventSchema
        else:
            self.schema = EventSchemaPublic

    def before_get_object(self, view_kwargs):
        """
        before get method to get the resource id for fetching details
        :param view_kwargs:
        :return:
        """
        get_id(view_kwargs)

        if view_kwargs.get('order_identifier') is not None:
            order = safe_query_kwargs(
                Order, view_kwargs, 'order_identifier', 'identifier'
            )
            if order.event_id is not None:
                view_kwargs['id'] = order.event_id
            else:
                view_kwargs['id'] = None

    def after_get_object(self, event, view_kwargs):
        if event and event.state == "draft":
            if not is_logged_in() or not has_access('is_coorganizer', event_id=event.id):
                raise ObjectNotFound({'parameter': '{id}'}, "Event: not found")

    def before_patch(self, args, kwargs, data=None):
        """
        before patch method to verify if the event location is provided before publishing the event and checks that
        the user is verified
        :param args:
        :param kwargs:
        :param data:
        :return:
        """
        user = User.query.filter_by(id=current_user.id).one()
        validate_event(user, data)

    def before_update_object(self, event, data, view_kwargs):
        """
        method to save image urls before updating event object
        :param event:
        :param data:
        :param view_kwargs:
        :return:
        """
        is_date_updated = (
            data.get('starts_at') != event.starts_at
            or data.get('ends_at') != event.ends_at
        )
        is_draft_published = event.state == "draft" and data.get('state') == "published"
        is_event_restored = event.deleted_at and not data.get('deleted_at')

        if is_date_updated or is_draft_published or is_event_restored:
            validate_date(event, data)

        if has_access('is_admin') and data.get('deleted_at') != event.deleted_at:
            if len(event.orders) != 0 and not has_access('is_super_admin'):
                raise ForbiddenError(
                    {'source': ''}, "Event associated with orders cannot be deleted"
                )
            event.deleted_at = data.get('deleted_at')

        if (
            data.get('original_image_url')
            and data['original_image_url'] != event.original_image_url
        ):
            start_image_resizing_tasks(event, data['original_image_url'])

    def after_update_object(self, event, data, view_kwargs):
        if event.state == 'published' and event.schedule_published_on:
            start_export_tasks(event)
        else:
            clear_export_urls(event)

    decorators = (
        api.has_permission(
            'is_coorganizer',
            methods="PATCH,DELETE",
            fetch="id",
            fetch_as="event_id",
            model=Event,
        ),
    )
    schema = EventSchema
    data_layer = {
        'session': db.session,
        'model': Event,
        'methods': {
            'before_update_object': before_update_object,
            'before_get_object': before_get_object,
            'after_get_object': after_get_object,
            'after_update_object': after_update_object,
            'before_patch': before_patch,
        },
    }


class EventRelationship(ResourceRelationship):
    """
    Event Relationship
    """

    def before_get_object(self, view_kwargs):
        if view_kwargs.get('identifier'):
            event = safe_query_kwargs(Event, view_kwargs, 'identifier', 'identifier')
            view_kwargs['id'] = event.id

    decorators = (
        api.has_permission(
            'is_coorganizer', fetch="id", fetch_as="event_id", model=Event
        ),
    )
    schema = EventSchema
    data_layer = {
        'session': db.session,
        'model': Event,
        'methods': {'before_get_object': before_get_object},
    }


class EventCopySchema(Schema):
    """
    API Schema for EventCopy
    """

    class Meta:
        """
        Meta class for EventCopySchema
        """

        type_ = 'event-copy'
        inflect = dasherize
        self_view = 'v1.event_copy'
        self_view_kwargs = {'identifier': '<id>'}

    id = fields.Str(dump_only=True)
    identifier = fields.Str(dump_only=True)


class EventCopyResource(ResourceList):
    """
    ResourceList class for EventCopy
    """

    schema = EventCopySchema
    methods = [
        'POST',
    ]
    data_layer = {'class': EventCopyLayer, 'session': db.Session}


def start_export_tasks(event):
    event_id = str(event.id)
    # XCAL
    from .helpers.tasks import export_xcal_task

    task_xcal = export_xcal_task.delay(event_id, temp=False)
    create_export_job(task_xcal.id, event_id)

    # ICAL
    from .helpers.tasks import export_ical_task

    task_ical = export_ical_task.delay(event_id, temp=False)
    create_export_job(task_ical.id, event_id)

    # PENTABARF XML
    from .helpers.tasks import export_pentabarf_task

    task_pentabarf = export_pentabarf_task.delay(event_id, temp=False)
    create_export_job(task_pentabarf.id, event_id)


def start_image_resizing_tasks(event, original_image_url):
    event_id = str(event.id)
    from .helpers.tasks import resize_event_images_task

    resize_event_images_task.delay(event_id, original_image_url)


def clear_export_urls(event):
    event.ical_url = None
    event.xcal_url = None
    event.pentabarf_url = None
    save_to_db(event)


class UpcomingEventList(EventList):
    """
    List Upcoming Events
    """

    def before_get(self, args, kwargs):
        """
        method for assigning schema based on admin access
        :param args:
        :param kwargs:
        :return:
        """
        super().before_get(args, kwargs)
        self.schema.self_view_many = 'v1.upcoming_event_list'

    def query(self, view_kwargs):
        """
        query method for upcoming events list
        :param view_kwargs:
        :return:
        """
        current_time = datetime.now(pytz.utc)
        query_ = (
            self.session.query(Event)
            .filter(
                Event.starts_at > current_time,
                Event.ends_at > current_time,
                Event.state == 'published',
                Event.privacy == 'public',
                or_(
                    Event.is_promoted,
                    and_(
                        Event.original_image_url != None,
                        Event.logo_url != None,
                        Event.event_type_id != None,
                        Event.event_topic_id != None,
                        Event.event_sub_topic_id != None,
                        Event.tickets.any(and_(Ticket.deleted_at == None, Ticket.is_hidden == False, Ticket.sales_ends_at > current_time)),
                        Event.social_link.any(SocialLink.name=="twitter")
                    ),
                ),
            )
            .order_by(Event.starts_at)
        )
        return query_

    data_layer = {
        'session': db.session,
        'model': Event,
        'methods': {'query': query},
    }
