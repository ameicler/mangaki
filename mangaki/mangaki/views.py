from django.views.generic.detail import DetailView, SingleObjectTemplateResponseMixin
from django.views.generic.list import ListView
from django.views.generic.edit import FormMixin
from django.views.generic import View
from django.views.defaults import server_error
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseForbidden, Http404, HttpResponsePermanentRedirect
from django.core.exceptions import SuspiciousOperation
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils import timezone
from django.utils.timezone import utc
from django.utils.functional import cached_property
from django.db.models import Case, When, Value, Sum, IntegerField
from django.views.generic.detail import SingleObjectMixin
from django.db import connection, DatabaseError

from mangaki.models import Work, Rating, ColdStartRating, Page, Profile, Artist, Suggestion, Recommendation, Pairing, Top, Ranking, Staff, Category, FAQTheme, Trope
from mangaki.mixins import AjaxableResponseMixin, JSONResponseMixin
from mangaki.forms import SuggestionForm
from mangaki.utils.mal import import_mal
from mangaki.utils.recommendations import get_reco_algo
from irl.models import Event, Partner, Attendee

from collections import Counter, OrderedDict
from markdown import markdown
from urllib.parse import urlencode
import datetime
import json

from mangaki.choices import TOP_CATEGORY_CHOICES

from natsort import natsorted

NB_POINTS_DPP = 10
POSTERS_PER_PAGE = 24
TITLES_PER_PAGE = 24
USERNAMES_PER_PAGE = 24
REFERENCE_DOMAINS = (
    ('http://myanimelist.net', 'myAnimeList'),
    ('http://animeka.com', 'Animeka'),
    ('http://vgmdb.net', 'VGMdb'),
    ('http://anidb.net', 'AniDB'),
    ('https://anidb.net', 'AniDB')
)

RATING_COLORS = {
    'favorite': {'normal': '#f8d549', 'highlight': '#f8d549'},
    'like': {'normal': '#5cb85c', 'highlight': '#47a447'},
    'neutral': {'normal': '#f0ad4e', 'highlight': '#ec971f'},
    'dislike': {'normal': '#d9534f', 'highlight': '#c9302c'},
    'willsee': {'normal': '#337ab7', 'highlight': '#286090'},
    'wontsee': {'normal': '#5bc0de', 'highlight': '#31b0d5'}
}

UTA_ID = 14293

GHIBLI_IDS = [2591, 8153, 2461, 53, 958, 30, 1563, 410, 60, 3315, 3177, 106]


def update_score_while_rating(user, work, choice):
    recommendations_list = Recommendation.objects.filter(target_user=user, work=work)
    for reco in recommendations_list:
        if choice == 'like':
            reco.user.profile.score += 1
        elif choice == 'favorite':
            reco.user.profile.score += 5
        if Rating.objects.filter(user=user, work=work, choice='like').count() > 0:
            reco.user.profile.score -= 1
        if Rating.objects.filter(user=user, work=work, choice='favorite').count() > 0:
            reco.user.profile.score -= 5
        Profile.objects.filter(user=reco.user).update(score=reco.user.profile.score)


def update_score_while_unrating(user, work, choice):
    recommendations_list = Recommendation.objects.filter(target_user=user, work=work)
    for reco in recommendations_list:
        if choice == 'like':
            reco.user.profile.score -= 1
            Profile.objects.filter(user=reco.user).update(score=reco.user.profile.score)
        elif choice == 'favorite':
            reco.user.profile.score -= 5
            Profile.objects.filter(user=reco.user).update(score=reco.user.profile.score)


class WorkDetail(AjaxableResponseMixin, FormMixin, SingleObjectTemplateResponseMixin, SingleObjectMixin, View):
    form_class = SuggestionForm
    queryset = Work.objects.select_related('category').prefetch_related('staff_set__role', 'staff_set__artist')

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.category.slug != self.kwargs.get('category'):
            return HttpResponsePermanentRedirect(self.object.get_absolute_url())

        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_success_url(self):
        return self.object.get_absolute_url()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        self.object.source = self.object.source.split(',')[0]

        context['genres'] = ', '.join(genre.title for genre in self.object.genre.all())

        if self.request.user.is_authenticated:
            context['suggestion_form'] = SuggestionForm(instance=Suggestion(user=self.request.user, work=self.object))
            try:
                context['rating'] = self.object.rating_set.get(user=self.request.user).choice
            except Rating.DoesNotExist:
                pass

        context['references'] = []
        for reference in self.object.reference_set.all():
            for domain, name in REFERENCE_DOMAINS:
                if reference.url.startswith(domain):
                    context['references'].append((reference.url, name))

        nb = Counter(Rating.objects.filter(work=self.object).values_list('choice', flat=True))
        labels = OrderedDict([
            ('favorite', 'Ajoutés aux favoris'),
            ('like',     'Ont aimé'),
            ('neutral',  'Neutre'),
            ('dislike',  'N\'ont pas aimé'),
            ('willsee',  'Ont envie de voir'),
            ('wontsee',  'N\'ont pas envie de voir'),
        ])
        seen_ratings = {'favorite', 'like', 'neutral', 'dislike'}
        total = sum(nb.values())
        if total > 0:
            context['stats'] = []
            seen_total = sum(nb[rating] for rating in seen_ratings)
            for rating, label in labels.items():
                if seen_total > 0 and rating not in seen_ratings:
                    continue
                context['stats'].append({'value': nb[rating], 'colors': RATING_COLORS[rating], 'label': label})
            context['seen_percent'] = round(100 * seen_total / float(total))

        events = self.object.event_set\
            .filter(date__gte=timezone.now())\
            .annotate(nb_attendees=Sum(Case(
                When(attendee__attending=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )))
        if len(events) > 0:
            my_events = {}
            if self.request.user.is_authenticated:
                my_events = dict(self.request.user.attendee_set.filter(
                    event__in=events).values_list('event_id', 'attending'))

            context['events'] = [
                {
                    'id': event.id,
                    'attending': my_events.get(event.id, None),
                    'type': event.get_event_type_display(),
                    'channel': event.channel,
                    'date': event.get_date(),
                    'link': event.link,
                    'location': event.location,
                    'nb_attendees': event.nb_attendees,
                } for event in events
            ]

        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not request.user.is_authenticated:
            return HttpResponseForbidden()
        form_class = self.get_form_class()
        form = self.get_form(form_class)

        if form.is_valid():
            return self.form_valid(form)
        return self.form_invalid(form)

    def form_valid(self, form):
        form.instance.user = self.request.user
        form.save()
        return super().form_valid(form)


class EventDetail(LoginRequiredMixin, DetailView):
    model = Event

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if 'next' in request.GET:
            return redirect(request.GET['next'])
        return redirect(self.object.work.get_absolute_url())

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        attending = None
        if 'wontgo' in request.POST:
            attending = False
        if 'willgo' in request.POST:
            attending = True
        if attending is not None:
            Attendee.objects.update_or_create(
                event=self.object, user=request.user,
                defaults={'attending': attending})
        elif 'cancel' in request.POST:
            Attendee.objects.filter(event=self.object, user=request.user).delete()
        return redirect(request.GET['next'])


class CardList(JSONResponseMixin, ListView):
    model = Work

    def get_queryset(self):
        category = self.kwargs.get('category')
        # This call to `int` shouldn't fail because `sort_id` must match the
        # `\d+` regexp, cf urls.py.
        sort_id = int(self.kwargs.pop('sort_id'))
        if sort_id < 1 or sort_id > 5:
            raise Http404
        deja_vu = self.request.GET.get('dejavu', '').split(',')
        sort_mode = ['popularity', 'controversy', 'top', 'random', 'dpp'][int(sort_id) - 1]
        queryset = Category.objects.get(slug=category).work_set.all()
        if sort_mode == 'popularity':
            queryset = queryset.popular()
        elif sort_mode == 'controversy':
            queryset = queryset.controversial()
        elif sort_mode == 'top':
            queryset = queryset.top()
        elif sort_mode == 'random':
            queryset.random().order_by('?')
        else:
            queryset = queryset.dpp(NB_POINTS_DPP)
        if self.request.user.is_authenticated:
            rated_works = self.request.user.rating_set.values('work_id')
            queryset = queryset.exclude(id__in=rated_works)
        return queryset[:POSTERS_PER_PAGE]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cards = []
        for work in context['object_list']:
            cards.append({
                'id': work.id,
                'category': work.category.slug,
                'title': work.title,
                'poster': work.safe_poster(self.request.user),
                'synopsis': work.synopsis,
                'nsfw': work.nsfw
            })
        return {'cards': cards}

    def render_to_response(self, context, **response_kwargs):
        return self.render_to_json_response(context, **response_kwargs)


class WorkListMixin:
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if self.request.user.is_authenticated:
            ratings = dict(
                Rating.objects.filter(
                    user=self.request.user,
                    work__in=list(context['object_list'])) \
                .values_list('work_id', 'choice'))
        else:
            ratings = {}
        for work in context['object_list']:
            work.rating = ratings.get(work.id, None)

        return context


class WorkList(WorkListMixin, ListView):
    paginate_by = POSTERS_PER_PAGE

    @cached_property
    def category(self):
        return get_object_or_404(Category, slug=self.kwargs.get('category'))

    def search(self):
        return self.request.GET.get('search', None)

    def sort_mode(self):
        default = 'mosaic'
        sort = self.request.GET.get('sort', default)
        if self.search() is not None and sort == default:
            return 'popularity' # Mosaic cannot be searched through because it is random. We enforce the popularity as the second default when searching.
        else:
            return sort

    # FIXME @property
    def is_dpp(self):
        dpp = self.kwargs.get('dpp', False)
        return dpp

    def get_queryset(self):
        search_text = self.search()
        queryset = self.category.work_set.all()
        sort_mode = self.sort_mode()
        if self.is_dpp():
            queryset = self.category.work_set.exclude(coldstartrating__user=self.request.user).dpp(10)
        elif sort_mode == 'top':
            queryset = queryset.top()
        elif sort_mode == 'popularity':
            queryset = queryset.popular()
        elif sort_mode == 'controversy':
            queryset = queryset.controversial()
        elif sort_mode == 'alpha':
            letter = self.request.GET.get('letter', '0')
            if letter == '0': # '#'
                queryset = queryset.exclude(title__regex=r'^[a-zA-Z]')
            else:
                queryset = queryset.filter(title__istartswith=letter)
            queryset = queryset.order_by('title')
        elif sort_mode == 'random':
            queryset = queryset.random().order_by('?')[:self.paginate_by]
        elif sort_mode == 'mosaic':
            queryset = queryset.none()
        else:
            raise Http404

        if search_text is not None:
            queryset = queryset.search(search_text)

        queryset = queryset.only('pk', 'title', 'ext_poster', 'nsfw', 'synopsis', 'category__slug')

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        search_text = self.search()
        sort_mode = self.sort_mode()
        is_dpp = self.is_dpp()

        context['search'] = search_text
        context['sort_mode'] = sort_mode
        context['letter'] = self.request.GET.get('letter', '')
        context['category'] = self.category.slug
        context['objects_count'] = self.category.work_set.count()
        context['is_dpp'] = is_dpp

        if sort_mode == 'mosaic' and not is_dpp:
            context['object_list'] = [
                Work(title='Chargement…', ext_poster='/static/img/chiro.gif')
                for _ in range(4)
                ]

        return context


class ArtistDetail(SingleObjectMixin, WorkListMixin, ListView):
    template_name = 'mangaki/artist_detail.html'
    paginate_by = POSTERS_PER_PAGE

    def get(self, request, *args, **kwargs):
        self.object = self.get_object(queryset=Artist.objects.all())
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return Work.objects.filter(id__in=Staff.objects.filter(artist=self.object).values('work_id')).order_by('title')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['artist'] = self.object

        return context


class UserList(ListView):
    model = User
    # context_object_name = 'anime'

    def get_queryset(self):
        bundle = User.objects.filter(profile__is_shared=True).order_by('-id')
        letter = self.request.GET.get('letter', '')
        if letter:
            if letter == '0':  # '#'
                bundle = bundle.exclude(username__regex=r'^[a-zA-Z]').order_by('username')
            else:
                bundle = bundle.filter(username__istartswith=letter).order_by('username')
        return bundle

    def get_context_data(self, **kwargs):
        context = super(UserList, self).get_context_data(**kwargs)

        letter = self.request.GET.get('letter', '')
        page = int(self.request.GET.get('page', '1'))
        context['object_list'] = list(context['object_list'])
        paginator = Paginator(context['object_list'], USERNAMES_PER_PAGE)
        try:
            user_list = paginator.page(page)
        except PageNotAnInteger:
            # If page is not an integer, deliver first page.
            user_list = paginator.page(1)
        except EmptyPage:
            # If page is out of range (e.g. 9999), deliver last page of results.
            user_list = paginator.page(paginator.num_pages)
        context['params'] = {'letter': letter, 'page': page}
        context['url'] = urlencode({'letter': letter})
        context['pages'] = filter(lambda x: 1 <= x <= paginator.num_pages, range(user_list.number - 2, user_list.number + 2 + 1))
        context['object_list'] = user_list

        context['trio_elm'] = User.objects.filter(username__in=['jj', 'Lily', 'Sedeto'])
        return context


def get_profile(request, username):
    user = get_object_or_404(User, username=username)
    is_shared = user.profile.is_shared
    category = request.GET.get('category', 'anime')
    ordering = ['favorite', 'willsee', 'like', 'neutral', 'dislike', 'wontsee']
    c = 0
    rating_list = natsorted(Rating.objects.filter(user__username=username).select_related('work'), key=lambda x: (ordering.index(x.choice), x.work.title.lower()))  # Tri par note puis nom

    received_recommendation_list = []
    sent_recommendation_list = []
    if category == 'recommendation':
        received_recommendations = Recommendation.objects.filter(target_user__username=username).select_related('work', 'work__category')
        sent_recommendations = Recommendation.objects.filter(user__username=username).select_related('work', 'work__category')
        for reco in received_recommendations:
            if Rating.objects.filter(work=reco.work, user__username=username, choice__in=['favorite', 'like', 'neutral', 'dislike']).count() == 0:
                received_recommendation_list.append({'category': reco.work.category.slug, 'id': reco.work.id, 'title': reco.work.title, 'username': reco.user.username})
        for reco in sent_recommendations:
            if Rating.objects.filter(work=reco.work, user=reco.target_user, choice__in=['favorite', 'like', 'neutral', 'dislike']).count() == 0:
                sent_recommendation_list.append({'category': reco.work.category.slug, 'id': reco.work.id, 'title': reco.work.title, 'username': reco.target_user.username})

    seen_lists = {'anime': [], 'manga': [], 'album': 0}
    unseen_lists = {'anime': [], 'manga': [], 'album': []}
    for r in rating_list:
        if r.choice in ['favorite', 'like', 'neutral', 'dislike']:
            seen_lists[r.work.category.slug].append(r)
        else:
            unseen_lists[r.work.category.slug].append(r)
    member_time = datetime.datetime.now().replace(tzinfo=utc) - user.date_joined

    # Events
    events = [
        {
            'id': attendee.event_id,
            'work_id': attendee.event.work_id,
            'attending': True,
            'type': attendee.event.get_event_type_display(),
            'channel': attendee.event.channel,
            'date': attendee.event.get_date(),
            'link': attendee.event.link,
            'location': attendee.event.location,
            'title': attendee.event.work.title,
        } for attendee in user.attendee_set.filter(event__date__gte=timezone.now(), attending=True).select_related('event', 'event__work')
    ]

    data = {
        'username': username,
        'score': user.profile.score,
        'is_shared': is_shared,
        'category': category,
        'avatar_url': user.profile.get_avatar_url(),
        'member_days': member_time.days,
        'anime_count': len(seen_lists['anime']),
        'manga_count': len(seen_lists['manga']),
        'reco_count': len(received_recommendation_list),
        'seen_list': seen_lists.get(category, []) if is_shared else [],
        'unseen_list': unseen_lists.get(category, []) if is_shared else [],
        'received_recommendation_list': received_recommendation_list if is_shared else [],
        'sent_recommendation_list': sent_recommendation_list if is_shared else [],
        'events': events,
    }
    return render(request, 'profile.html', data)


def index(request):
    if request.user.is_authenticated:
        if Rating.objects.filter(user=request.user).count() == 0:
            return redirect('/anime/')
    # texte = Announcement.objects.get(title='Flash News').text
    # context = {'annonce': texte}
    partners = Partner.objects.filter()
    return render(request, 'index.html', {
        'partners': partners,
    })


def about(request):
    return render(request, 'about.html')


def events(request):
    uta_rating = None
    if request.user.is_authenticated:
        for rating in Rating.objects.filter(work_id=UTA_ID, user=request.user):
            if rating.work_id == UTA_ID:
                uta_rating = rating.choice
    ghibli_works = Work.objects.in_bulk(GHIBLI_IDS)
    if request.user.is_authenticated:
        ghibli_ratings = dict(Rating.objects.filter(user=request.user, work_id__in=GHIBLI_IDS).values_list('work_id', 'choice'))
    else:
        ghibli_ratings = {}
    utamonogatari = Work.objects.in_bulk([UTA_ID])
    return render(
        request, 'events.html',
        {
            'screenings': Event.objects.filter(event_type='screening', date__gte=timezone.now()),
            'ghibli': [(ghibli_works.get(work_id, None), ghibli_ratings.get(work_id, None)) for work_id in GHIBLI_IDS],
            'utamonogatari': utamonogatari.get(UTA_ID, None),
            'wakanim': Partner.objects.get(pk=12),
            'utamonogatari_rating': uta_rating,
        })


def top(request, category_slug):
    categories = dict(TOP_CATEGORY_CHOICES)
    if category_slug not in categories:
        raise Http404
    try:
        top = Top.objects.filter(category=category_slug).latest('date')
    except Top.DoesNotExist:
        raise Http404
    data = []
    rankings = Ranking.objects.filter(top=top).prefetch_related('content_object')
    for rank, ranking in enumerate(rankings):
        data.append({
            'rank': rank + 1,
            'id': ranking.object_id,
            'name': str(ranking.content_object),
            'score': ranking.score,
            'nb_ratings': ranking.nb_ratings,
            'nb_stars': ranking.nb_stars,
        })
    return render(request, 'top.html', {
        'date': top.date,
        'size': len(data),
        'category_slug': category_slug,
        'category': categories[category_slug].lower(),
        'top': data,
    })

def rate_work(request, work_id):
    if request.user.is_authenticated and request.method == 'POST':
        work = get_object_or_404(Work, id=work_id)
        choice = request.POST.get('choice', '')
        if choice not in ['like', 'neutral', 'dislike', 'willsee', 'wontsee', 'favorite']:
            return HttpResponse()
        if Rating.objects.filter(user=request.user, work=work, choice=choice).count() > 0:
            Rating.objects.filter(user=request.user, work=work, choice=choice).delete()
            update_score_while_unrating(request.user, work, choice)
            return HttpResponse('none')
        update_score_while_rating(request.user, work, choice)
        Rating.objects.update_or_create(user=request.user, work=work, defaults={'choice': choice})
        return HttpResponse(choice)
    return HttpResponse()


# FIXME @login_required
def dpp_work(request, work_id):
    if request.user.is_authenticated() and request.method == 'POST':
        work = get_object_or_404(Work, id=work_id)
        choice = request.POST.get('choice', '')
        if choice not in ['like', 'dislike', 'dontknow']:
            raise SuspiciousOperation("Attempted access denied. There are only 3 ratings here: like, dislike and dontknow")
        ColdStartRating.objects.update_or_create(user=request.user, work=work, defaults={'choice': choice})
        return HttpResponse(choice)
    raise SuspiciousOperation("Attempted access denied. You are not logged in or it is currently a GET request")


def recommend_work(request, work_id, target_id):
    if request.user.is_authenticated and request.method == 'POST':
        work = get_object_or_404(Work, id=work_id)
        target_user = get_object_or_404(User, id=target_id)
        if target_user == request.user:
            return HttpResponse('nonsense')
        if Recommendation.objects.filter(user=request.user, work=work, target_user=target_user).count() > 0:
            return HttpResponse('double')
        if not Rating.objects.filter(user=target_user, work=work, choice__in=['favorite', 'like', 'neutral', 'dislike']):
            Recommendation.objects.update_or_create(user=request.user, work=work, target_user=target_user)
            return HttpResponse('success')
    return HttpResponse()


def get_users(request, query=''):
    data = []
    for user in User.objects.all() if not query else User.objects.filter(username__icontains=query):
        data.append({'id': user.id, 'username': user.username, 'tokens': user.username.lower().split()})
    return HttpResponse(json.dumps(data), content_type='application/json')


def get_user_for_recommendations(request, work_id, query=''):
    data = []
    for user in User.objects.all() if not query else User.objects.filter(username__icontains=query):
        data.append({'id': user.id, 'username': user.username, 'work_id': work_id, 'tokens': user.username.lower().split()})
    return HttpResponse(json.dumps(data), content_type='application/json')


class MarkdownView(DetailView):
    model = Page
    slug_field = 'name'
    template_name = 'static.html'

    def get_context_data(self, **kwargs):
        return {'html': markdown(self.object.markdown)}


def get_works(request, category):
    query = request.GET.get('q', '')
    data = [
        {
            'id': work.id,
            'synopsis': work.synopsis[:50] + '…',
            'title': work.title,
            'year': '' if not work.date else work.date.year,
        } for work in Work.objects.filter(category__slug=category).search(query).popular()[:10]
    ]
    return HttpResponse(json.dumps(data), content_type='application/json')


@login_required
def get_reco_algo_list(request, algo, category):
    reco_list = []
    data = get_reco_algo(request.user, algo, category)
    works = data['works']
    for work_id in data['work_ids']:
        work = works[work_id]
        reco_list.append({'id': work.id, 'title': work.title, 'poster': work.ext_poster, 'synopsis': work.synopsis, 'category': work.category.slug})
    return HttpResponse(json.dumps(reco_list), content_type='application/json')

def get_reco_list_dpp(request, category):
    reco_list_dpp = []
    data = get_reco_algo(request.user, 'knn', category)
    works = data['works']
    for work_id in data['work_ids']:
        work = works[work_id]
        reco_list_dpp.append({'id': work.id, 'title': work.title, 'poster': work.ext_poster, 'synopsis': work.synopsis, 'category': work.category.slug})
    return HttpResponse(json.dumps(reco_list_dpp), content_type='application/json')


def remove_reco(request, work_id, username, targetname):
    work = get_object_or_404(Work, id=work_id)
    user = get_object_or_404(User, username=username)
    target = get_object_or_404(User, username=targetname)
    if Rating.objects.filter(user=target, work=work, choice__in=['favorite', 'like', 'neutral', 'dislike']).count() == 0 and (request.user == user or request.user == target):
        Recommendation.objects.get(work=work, user=user, target_user=target).delete()


def remove_all_reco(request, targetname):
    target = get_object_or_404(User, username=targetname)
    if target == request.user:
        reco_list = Recommendation.objects.filter(target_user=target)
        for reco in reco_list:
            if Rating.objects.filter(user=request.user, work=reco.work, choice__in=['favorite', 'like', 'neutral', 'dislike']).count() == 0:
                reco.delete()


@login_required
def get_reco(request):
    category = request.GET.get('category', 'all')
    algo = request.GET.get('algo', 'knn')
    if request.user.rating_set.exists():
        reco_list = [Work(title='Chargement…', ext_poster='/static/img/chiro.gif') for _ in range(4)]
    else:
        reco_list = []
    return render(request, 'mangaki/reco_list.html', {'reco_list': reco_list, 'category': category, 'algo': algo})


@login_required
def get_reco_dpp(request):
    category = request.GET.get('category', 'all')
    reco_list = [Work(title='Chargement…', ext_poster='/static/img/chiro.gif') for _ in range(4)]
    return render(request, 'mangaki/reco_list_dpp.html', {'reco_list': reco_list, 'category': category})


def update_shared(request):
    if request.user.is_authenticated and request.method == 'POST':
        Profile.objects.filter(user=request.user).update(is_shared=request.POST['is_shared'] == 'true')
    return HttpResponse()


def update_nsfw(request):
    if request.user.is_authenticated and request.method == 'POST':
        Profile.objects.filter(user=request.user).update(nsfw_ok=request.POST['nsfw_ok'] == 'true')
    return HttpResponse()


def update_newsletter(request):
    if request.user.is_authenticated and request.method == 'POST':
        Profile.objects.filter(user=request.user).update(newsletter_ok=request.POST['newsletter_ok'] == 'true')
    return HttpResponse()


def update_reco_willsee(request):
    if request.user.is_authenticated and request.method == 'POST':
        Profile.objects.filter(user=request.user).update(reco_willsee_ok=request.POST['reco_willsee_ok'] == 'true')
    return HttpResponse()


def import_from_mal(request, mal_username):
    if request.method == 'POST':
        nb_added, fails = import_mal(mal_username, request.user.username)
        return HttpResponse('%d added; %d fails: %s' % (nb_added, len(fails), '\n'.join(fails)))
    return HttpResponse()


def add_pairing(request, artist_id, work_id):
    if request.user.is_authenticated:
        artist = get_object_or_404(Artist, id=artist_id)
        work = get_object_or_404(Work, id=work_id)
        Pairing(user=request.user, artist=artist, work=work).save()
    return HttpResponse()


def faq_index(request):
    latest_theme_list = FAQTheme.objects.order_by('order')
    all_information = [[faqtheme.theme, [(entry.question, entry.answer) for entry in faqtheme.entries.filter(is_active=True).order_by('-pub_date')]] for faqtheme in latest_theme_list]
    context = {
        'information': all_information,
    }
    return render(request, 'faq/faq_index.html', context)

def generic_error_view(error, error_code):
    def error_view(request):
        try:
            trope = Trope.objects.order_by('?').first()
        except DatabaseError:
            return server_error(request)

        parameters = {
            'error_code': error_code,
            'error': error,
        }
        if trope:
            parameters['trope'] = trope
            parameters['origin'] = trope.origin
        return render(request, 'error.html', parameters, status=error_code)
    return error_view
