"""View definitions for the Checkouts app"""
import logging

from django.contrib import messages
from django.contrib.auth.models import User
from django.db.models import Count
from django.shortcuts import render # Used for rendering 403 Forbidden
from django.views.generic import DetailView, ListView, TemplateView

from braces.views import LoginRequiredMixin

from .forms import FilterForm, CheckoutEditForm
from .models import AircraftType, Airstrip, Checkout
import util


logger = logging.getLogger(__name__)


class PilotList(LoginRequiredMixin, ListView):
    """List of current pilots"""
    queryset = util.get_pilots()
    context_object_name = 'pilot_list'
    template_name = 'checkouts/pilot_list.html'
    

class PilotDetail(LoginRequiredMixin, DetailView):
    """All checkout information for a particular pilot"""
    model = User
    context_object_name = 'pilot'
    template_name = 'checkouts/pilot_detail.html'
    slug_field = 'username'
    slug_url_kwarg = 'username'
    
    def get_context_data(self, **kwargs):
        context = super(PilotDetail, self).get_context_data(**kwargs)
        
        context['checkouts'] = util.pilot_checkouts_grouped_by_airstrip(self.object)
        
        return context


class AirstripList(LoginRequiredMixin, ListView):
    """List of current airstrips"""
    queryset = Airstrip.objects.all().order_by('ident')
    context_object_name = 'airstrip_list'
    template_name = 'checkouts/airstrip_list.html'


class AirstripDetail(LoginRequiredMixin, DetailView):
    """All checkout information for a particular airstrip"""
    model = Airstrip
    context_object_name = 'airstrip'
    template_name = 'checkouts/airstrip_detail.html'
    slug_field = 'ident'
    slug_url_kwarg = 'ident'
    
    def get_context_data(self, **kwargs):
        context = super(AirstripDetail, self).get_context_data(**kwargs)
        
        context['checkouts'] = util.airstrip_checkouts_grouped_by_pilot(self.object)
        
        return context


class BaseList(LoginRequiredMixin, ListView):
    """List of airstrips which are bases"""
    queryset = util.get_bases().annotate(attached=Count('airstrip'))
    template_name = 'checkouts/base_list.html'
    
    def get_context_data(self, **kwargs):
        context = super(BaseList, self).get_context_data(**kwargs)
        
        bases = self.object_list
        base_list = []
        for base in bases:
            unattached = base.unattached_airstrips().count()
            base_list.append((base, base.attached, unattached))
        
        context['base_list'] = base_list
        
        return context


class BaseAttachedDetail(LoginRequiredMixin, DetailView):
    """List of airstrips which are attached to a given base"""
    model = Airstrip
    context_object_name = 'base'
    template_name = 'checkouts/base_detail.html'
    slug_field = 'ident'
    slug_url_kwarg = 'ident'
    
    def get_context_data(self, **kwargs):
        context = super(BaseAttachedDetail, self).get_context_data(**kwargs)
        context['attached_state'] = "attached"
        context['airstrips'] = self.object.attached_airstrips()
        
        return context


class BaseUnattachedDetail(LoginRequiredMixin, DetailView):
    """List of airstrips which are not attached to a given base"""
    model = Airstrip
    context_object_name = 'base'
    template_name = 'checkouts/base_detail.html'
    slug_field = 'ident'
    slug_url_kwarg = 'ident'
    
    def get_context_data(self, **kwargs):
        context = super(BaseUnattachedDetail, self).get_context_data(**kwargs)
        context['attached_state'] = "unattached"
        context['airstrips'] = self.object.unattached_airstrips()
        
        return context


class BaseEditAttached(LoginRequiredMixin, DetailView):
    """Allows users to edit the set of airstrips attached to a base"""
    context_object_name = 'base'
    model = Airstrip
    # Only bases can have attached airstrips
    queryset = util.get_bases()
    slug_field = 'ident'
    slug_url_kwarg = 'ident'
    template_name = 'checkouts/base_edit.html'
    
    def forbidden(self, request, message="Sorry, you can't do that."):
        """Shortcut for rendering an 'access denied' page"""
        template = '403.html'
        context = {
            'reason': message,
        }
        return render(request, template, context, status=403)
    
    def get(self, request, *args, **kwargs):
        """Renders a fresh form for editing, assuming the user is authorized"""
        logger.debug("=> BaseEditAttached.get")
        
        # Security Check
        # --------------
        # This would be unusual, but just in case: make sure that the request
        # is from a superuser.
        if request.user.is_superuser:
            return super(BaseEditAttached, self).get(request, *args, **kwargs)
        else:
            username = request.user.username
            logger.warn("Forbidden: '%s' attempted to edit base attachments without 'superuser' status" % username)
            message = "Only admins may modify which airstrips are attached to a base."""
            return self.forbidden(request, message)
    
    def get_context_data(self, **kwargs):
        """The form needs the full set of airstrips (excluding the 'self' base),
        and the set of airstrips currently attached to the 'self' base."""
        context = super(BaseEditAttached, self).get_context_data(**kwargs)
        context['airstrips'] = Airstrip.objects.exclude(pk=self.object.id).order_by('ident')
        context['attached'] = self.object.attached_airstrips()
        
        return context
    
    def post(self, request, *args, **kwargs):
        """Updates the set of airstrips attached to the given base."""
        base = self.get_object()
        logger.debug("=> BaseEditAttached.post for %s" % base.ident)
        
        # Security Check
        # --------------
        # This would be unusual, but just in case: make sure that the request
        # is from a superuser.
        if not request.user.is_superuser:
            username = request.user.username
            logger.warn("Forbidden: '%s' attempted to save base attachments without 'superuser' status" % username)
            message = "Only admins may modify which airstrips are attached to a base."""
            return self.forbidden(request, message)
        
        logger.debug(request.POST)
        
        current = base.attached_airstrips()
        logger.debug("Current: %d attached airstrips" % current.count())
        proposed = Airstrip.objects.filter(ident__in=request.POST.getlist('airstrip', []))
        logger.debug("Proposed: %d attached airstrips" % proposed.count())
        
        # We can figure out what changes to make by comparing the 'current'
        # attachments with the 'proposed' list.
        to_add = [airstrip for airstrip in proposed]
        to_delete = []
        for airstrip in current:
            if airstrip in to_add:
                to_add.remove(airstrip)
            else:
                to_delete.append(airstrip)
        
        # Prevent the user from adding a self-loop on a base
        if base in to_add:
            message = "Unable to set attachment for '%s' to itself" % base.name
            messages.add_message(request, messages.ERROR, message)
            to_add.remove(base)
        
        if not to_add and not to_delete:
            message = "Nothing updated; No changes necessary."
            messages.add_message(request, messages.SUCCESS, message)
        else:
            if to_add:
                for airstrip in to_add:
                    airstrip.bases.add(base)
                updates = ', '.join([airstrip.name for airstrip in to_add])
                logger.info("%s is attaching the following to %s: %s" % (request.user.username, base.ident, updates))
                message = "Attached: %s" % updates
                messages.add_message(request, messages.SUCCESS, message)
            
            if to_delete:
                for airstrip in to_delete:
                    airstrip.bases.remove(base)
                updates = ', '.join([airstrip.name for airstrip in to_delete])
                logger.info("%s is detaching the following from %s: %s" % (request.user.username, base.ident, updates))
                message = "Unattached: %s" % updates 
                messages.add_message(request, messages.SUCCESS, message)
        
        # We'll render a fresh view of the editing form which will have all
        # the updates in place.
        return self.get(request, *args, **kwargs)

class FilterFormView(LoginRequiredMixin, TemplateView):
    """Provides an interface for filtering the set of checkout records
    
    Not limited to existing records: it's possible to query for 'which
    checkouts have not been completed?'
    """
    form_class = FilterForm
    template_name = 'checkouts/filter.html'
    
    def get(self, request, *args, **kwargs):
        """Renders a fresh filter form"""
        logger.debug("=> FilterFormView.get")
        context = {'form': self.form_class(),}
        return self.render_to_response(context)
    
    def post(self, request, *args, **kwargs):
        """If the filter is valid, renders the filtered checkout data"""
        logger.debug("=> FilterFormView.post")
        logger.debug(request.POST)
        form = self.form_class(request.POST)
        context = {'form': form}
        
        if not form.is_valid():
            logger.debug("Unable to validate form data")
        else:
            logger.debug(form.cleaned_data)
            status = form.cleaned_data['checkout_status']
            if status == util.CHECKOUT_SUDAH:
                context['checkouts'] = util.sudah_selesai(**form.cleaned_data)
            else:
                context['checkouts'] = util.belum_selesai(**form.cleaned_data)
            
            context['show_summary'] = True
            
        return self.render_to_response(context)


class CheckoutEditFormView(LoginRequiredMixin, TemplateView):
    """Provides an interface for adding and removing checkouts"""
    form_class = CheckoutEditForm
    template_name = 'checkouts/edit.html'
    
    def forbidden(self, request, message="Sorry, you can't do that."):
        """Shortcut for rendering an 'access denied' page"""
        template = '403.html'
        context = {
            'reason': message,
        }
        return render(request, template, context, status=403)
    
    def get(self, request, *args, **kwargs):
        """Renders a fresh form instance"""
        logger.debug("=> CheckoutEditFormView.get")
        
        # Security Check
        # --------------
        # This would be unusual, but just in case: make sure that the request
        # is from a superuser or a pilot (normal users may not edit checkouts).
        if not request.user.is_superuser and not request.user.is_pilot:
            username = request.user.username
            logger.warn("Forbidden: '%s' is neither a pilot nor a superuser" % username)
            message = 'Only pilots may edit checkouts.'
            return self.forbidden(request, message)
        
        # Help out a pilot by pre-selecting their name
        if request.user.is_pilot:
            form = self.form_class(initial={'pilot': request.user})
        else:
            form = self.form_class()
        
        # Pilots may only edit their own checkouts if they are not a superuser
        if not request.user.is_superuser:
            form['pilot'].field.queryset = User.objects.filter(pk=request.user.id)
        
        return self.render_to_response({'form': form})
    
    def post(self, request, *args, **kwargs):
        """Given a valid form, performs the requested add/remove action and
        then renders the same view again."""
        logger.debug("=> CheckoutEditFormView.post")
        
        # Security Check
        # --------------
        # This would be unusual, but just in case: make sure that the request
        # is from a superuser or a pilot (normal users may not edit checkouts).
        if not request.user.is_superuser and not request.user.is_pilot:
            username = request.user.username
            logger.warn("Forbidden: '%s' is neither a pilot nor a superuser" % username)
            message = 'Only pilots may edit checkouts.'
            return self.forbidden(request, message)
        
        # Note that we're not doing anything with the submitted data until
        # _after_ the user has been verified as someone who can access this
        # view.
        logger.debug(request.POST)
        form = self.form_class(request.POST)
        context = {'form': form}
        
        if not form.is_valid():
            logger.debug("Unable to validate form data: %s" % form.errors)
            messages.add_message(
                        request, 
                        messages.ERROR, 
                        "Sorry, please check below for any error messages.")
        else:
            logger.debug(form.cleaned_data)
            pilot = form.cleaned_data['pilot']
            airstrip = form.cleaned_data['airstrip']
            aircraft_types = form.cleaned_data['aircraft_type']
            action = request.POST['action']
            
            # Security Check
            # --------------
            # This would be unusual, but just in case: if the user is not a
            # superuser, they may only edit their own checkouts
            if pilot != request.user and not request.user.is_superuser:
                username = request.user.username
                logger.warn("Forbidden: '%s' may not edit for '%s'" % (username, pilot.username))
                message = 'Pilots may only edit their own checkouts.'
                return self.forbidden(request, message)
            
            if action == u'Remove Checkout':
                checkouts = Checkout.objects.filter(
                                pilot=pilot, 
                                airstrip=airstrip, 
                                aircraft_type__in=aircraft_types)
                
                for c in checkouts:
                    logger.info("%s is deleting '%s'" % (request.user.username, c))
                    # We'll be pretending that all of the requested checkouts
                    # were deleted, even if they never existed, so we need to
                    # keep track of which ones haven't really been deleted.
                    # Then we'll use those remaining 'checkouts' to construct
                    # 'delete successful' messages for the user.
                    aircraft_types = aircraft_types.exclude(name=c.aircraft_type)
                    c.delete()
                    messages.add_message(request, messages.SUCCESS, "Deleted '%s'" % c)
                
                # Now that all the checkouts which actually existed have been
                # deleted, we'll build 'delete successful' messages for each
                # of the remainders.
                if aircraft_types.count() > 0:
                    for ac_type in aircraft_types:
                        c = "%s is checked out at %s in a %s" % (pilot, airstrip, ac_type)
                        logger.info("Pretending to delete non-existent checkout '%s'" % c)
                        messages.add_message(request, messages.SUCCESS, "Deleted '%s'" % c)
                
            else:
                for ac_type in aircraft_types:
                    existing = Checkout.objects.filter(
                                    pilot=pilot, 
                                    airstrip=airstrip, 
                                    aircraft_type=ac_type)
                    
                    # Don't allow a duplicate checkout to be created. Unlike
                    # the 'delete checkout' version, here we'll actually tell
                    # the user that we found a duplicate (although it's still
                    # styled as a 'success' message).
                    if existing.exists():
                        c = existing.get()
                        logger.debug("Prevented duplicate '%s'" % c)
                        messages.add_message(
                                    request, 
                                    messages.SUCCESS, 
                                    "Already exists: '%s'" % c)
                    else:
                        c = Checkout(pilot=pilot, airstrip=airstrip, aircraft_type=ac_type)
                        logger.info("%s is adding '%s'" % (request.user.username, c))
                        c.save()
                        messages.add_message(request, messages.SUCCESS, "Added '%s'" % c)
        
        return self.render_to_response(context)
