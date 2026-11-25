/*
 * Patch Status: search, filter and sort the page body (.ps) that
 * portal/patch_status/render.py writes. The search state is in the URL
 * (?q=...&age=...), so a search can be linked.
 */
(function () {
var root = document.querySelector('.ps');
if (!root) return;
var q = root.querySelector('.filter input'), age = root.querySelector('.filter select');
var hits = root.querySelector('.hits'), list = root.querySelector('#ps-suggest');

/* ---- rows ---- */

var FIELDS = {owner: 'owner', org: 'org', component: 'component', issue: 'issue',
	type: 'type', tag: 'topic', comment: 'comment', change: 'change'};
var rows = [];
var sections = [].map.call(root.querySelectorAll('section'), function (el) {
	var mine = [].map.call(el.querySelectorAll('tbody tr'), function (tr, i) {
		return {el: tr, i: i, ps: tr.dataset.ps, blocked: tr.className === 'blocked',
			text: [].map.call(tr.cells, function (c) { return c.textContent; }).join(' ').toLowerCase()};
	});
	rows = rows.concat(mine);
	return {el: el, rows: mine, n: el.querySelector('h2 .n'),
		sum: root.querySelector('.sum tr[data-bucket="' + el.id + '"] .n')};
});

function values(row, key) {
	row.f = row.f || {};
	if (row.f[key]) return row.f[key];
	var cell = row.el.querySelector('.c-' + FIELDS[key]), t = cell ? cell.textContent.trim() : '', out = [t];
	if (key === 'owner') out = [t.replace(/ [+-]\d$/, '')];
	else if (key === 'tag') out = [].map.call(cell.querySelectorAll('a'), function (a) { return a.textContent; });
	else if (key === 'comment') out = t.split(' · ');
	else if (key === 'change') out = [cell.querySelector('a').textContent.split('/')[0]];
	return row.f[key] = out;
}

/* ---- fuzzy matching ---- */

/* Edits needed to turn `w` into the best-fitting prefix of `t`, up to `max`. */
function distance(w, t, max) {
	var n = Math.min(t.length, w.length + max), prev2 = null, prev = [], i, j;
	for (j = 0; j <= n; j++) prev[j] = j;
	for (i = 1; i <= w.length; i++) {
		var cur = [i], low = i;
		for (j = 1; j <= n; j++) {
			var d = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (w[i - 1] !== t[j - 1]));
			if (i > 1 && j > 1 && w[i - 1] === t[j - 2] && w[i - 2] === t[j - 1])
				d = Math.min(d, prev2[j - 2] + 1);
			cur[j] = d;
			if (d < low) low = d;
		}
		if (low > max) return max + 1;
		prev2 = prev; prev = cur;
	}
	return Math.min.apply(null, prev);
}

/* Numbers and short words are never corrected. */
function tolerance(w) {
	return /\d/.test(w) || w.length < 4 ? 0 : w.length < 8 ? 1 : 2;
}

var words;
function closest(w) {
	var max = tolerance(w), best = max, out = [];
	if (!max) return out;
	if (!words) {
		var seen = {};
		rows.forEach(function (r) {
			(r.text.match(/[a-z][a-z0-9_]{2,}/g) || []).forEach(function (x) {
				seen[x] = (seen[x] || 0) + 1;
			});
		});
		words = Object.keys(seen).map(function (x) { return [x, seen[x]]; });
	}
	words.forEach(function (x) {
		if (x[0].length < w.length - max) return;
		var d = distance(w, x[0], best);
		if (d < best) { best = d; out = []; }
		if (d === best) out.push(x);
	});
	return out.sort(function (a, b) { return b[1] - a[1]; }).slice(0, 8).map(function (x) { return x[0]; });
}

/* ---- query ---- */

var TOKEN = /(\w+):"([^"]*)"?|"([^"]*)"?|(\S+)/g;

function parse(text) {
	var out = [], m;
	TOKEN.lastIndex = 0;
	while ((m = TOKEN.exec(text))) {
		var key = m[1], val = m[2], plain = m[3] !== undefined ? m[3] : m[4];
		if (key && FIELDS[key]) { out.push({key: key, val: val.toLowerCase(), exact: true}); continue; }
		if (key) plain = key + ':' + val;
		var c = plain.match(/^(\w+):(.+)$/);
		if (c && FIELDS[c[1]]) out.push({key: c[1], val: c[2].toLowerCase()});
		else if (plain) out.push({word: plain.toLowerCase(), quoted: m[3] !== undefined});
	}
	return out;
}

function tester(t) {
	if (t.key) return function (r) {
		return values(r, t.key).some(function (v) {
			v = v.toLowerCase();
			return t.exact ? v === t.val : v.indexOf(t.val) >= 0;
		});
	};
	var any = t.quoted || rows.some(function (r) { return r.text.indexOf(t.word) >= 0; });
	t.instead = any ? [] : closest(t.word);
	var alt = t.instead.length ? t.instead : [t.word];
	return function (r) { return alt.some(function (w) { return r.text.indexOf(w) >= 0; }); };
}

function apply() {
	var terms = parse(q.value), tests = terms.map(tester), min = '';
	if (age.value) min = new Date(Date.now() - age.value * 864e5).toISOString().slice(0, 10);
	var active = terms.length > 0 || !!min, shown = 0, all = 0;
	sections.forEach(function (s) {
		var n = 0, total = 0, visible = false;
		s.rows.forEach(function (r) {
			var ok = (!min || r.ps >= min) && tests.every(function (f) { return f(r); });
			r.el.hidden = !ok;
			visible = visible || ok;
			if (!r.blocked) { total++; if (ok) n++; }
		});
		shown += n; all += total;
		var label = active ? n + ' of ' + total : String(total);
		s.n.textContent = label;
		if (s.sum) s.sum.textContent = label;
		s.el.hidden = active && !visible;
	});
	var fixed = terms.filter(function (t) { return t.instead && t.instead.length; }).map(function (t) {
		return 'no “' + t.word + '”, showing ' + t.instead.slice(0, 3).join(', ');
	});
	hits.textContent = active ? shown + ' of ' + all + ' changes' + (fixed.length ? ' — ' + fixed.join('; ') : '') : '';
	var p = new URLSearchParams(location.search);
	p.delete('q'); p.delete('age');
	if (q.value.trim()) p.set('q', q.value.trim());
	if (age.value) p.set('age', age.value);
	try {
		history.replaceState(null, '', location.pathname + (String(p) ? '?' + p : '') + location.hash);
	} catch (e) {}
}

/* ---- suggestions ---- */

var entries, shownEntries = [], active = -1;

function vocabulary() {
	if (entries) return entries;
	var seen = {};
	entries = [];
	function add(key, val, label) {
		var id = key + '\0' + val;
		if (!seen[id]) {
			seen[id] = {key: key, val: val, label: label || val, count: 0};
			seen[id].low = seen[id].label.toLowerCase();
			seen[id].parts = seen[id].low.split(/[^a-z0-9]+/).filter(Boolean);
			entries.push(seen[id]);
		}
		seen[id].count++;
	}
	rows.forEach(function (r) {
		if (r.blocked) return;
		['owner', 'org', 'component', 'issue', 'type', 'tag'].forEach(function (k) {
			values(r, k).forEach(function (v) { if (v) add(k, v); });
		});
		values(r, 'comment').forEach(function (v) {
			if (v && !/#|unresolved/.test(v)) add('comment', v);
		});
		add('change', values(r, 'change')[0],
			values(r, 'change')[0] + ' ' + r.el.querySelector('.c-summary').textContent);
	});
	return entries;
}

/* Lower is better; 99 is no match. */
function score(e, w) {
	if (e.low === w) return 0;
	if (e.low.indexOf(w) === 0) return 1;
	if (e.parts.some(function (p) { return p.indexOf(w) === 0; })) return 2;
	if (e.low.indexOf(w) >= 0) return 3;
	var max = tolerance(w), best = max + 1;
	if (max) e.parts.forEach(function (p) {
		if (p.length >= w.length - max) best = Math.min(best, distance(w, p, max));
	});
	return best <= max ? 4 + best : 99;
}

/* The word being typed, which a suggestion replaces. */
function tail() {
	var before = q.value.slice(0, q.selectionStart === null ? q.value.length : q.selectionStart);
	var m = before.match(/(?:^|\s)((?:(\w+):)?"?([^"\s]*))$/);
	if (!m || !m[1]) return null;
	var key = m[2] && FIELDS[m[2]] ? m[2] : null;
	var word = key ? m[3] : m[1].replace(/^"/, '');
	return {start: before.length - m[1].length, end: before.length, key: key, word: word.toLowerCase()};
}

function suggest() {
	var t = tail();
	shownEntries = [];
	if (t && (t.word.length > 1 || t.key)) {
		var rank = {owner: 0, tag: 1, component: 2, issue: 3, comment: 4, org: 5, type: 6, change: 7};
		var found = [];
		vocabulary().forEach(function (e) {
			if (t.key && e.key !== t.key) return;
			var s = t.word ? score(e, t.word) : 1;
			if (s < 99) found.push({e: e, s: s + (e.key === 'change' ? 0.5 : 0)});
		});
		found.sort(function (a, b) {
			return a.s - b.s || b.e.count - a.e.count || rank[a.e.key] - rank[b.e.key] ||
				(a.e.low < b.e.low ? -1 : 1);
		});
		/* corrections only when nothing matches as typed */
		if (found.length && found[0].s < 4) found = found.filter(function (x) { return x.s < 4; });
		var patches = 0;
		shownEntries = found.filter(function (x) {
			return x.e.key !== 'change' || ++patches <= 4;
		}).slice(0, 10).map(function (x) { return x.e; });
	}
	list.textContent = '';
	active = -1;
	shownEntries.forEach(function (e, i) {
		var li = document.createElement('li');
		li.id = 'ps-s' + i;
		li.setAttribute('role', 'option');
		[['kind', e.key === 'tag' ? 'hashtag' : e.key], ['what', e.label],
			['count', e.key === 'change' ? '' : e.count]].forEach(function (x) {
			var s = document.createElement('span');
			s.className = x[0]; s.textContent = x[1];
			li.appendChild(s);
		});
		/* mousedown: before the input loses focus */
		li.addEventListener('mousedown', function (ev) { ev.preventDefault(); choose(i); });
		list.appendChild(li);
	});
	open(shownEntries.length > 0);
}

function open(yes) {
	list.hidden = !yes;
	q.setAttribute('aria-expanded', String(yes));
	if (!yes) { active = -1; q.removeAttribute('aria-activedescendant'); }
}

function move(step) {
	var n = shownEntries.length;
	active = active < 0 ? (step > 0 ? 0 : n - 1) : (active + step + n) % n;
	[].forEach.call(list.children, function (li, i) {
		li.setAttribute('aria-selected', String(i === active));
		if (i === active) { q.setAttribute('aria-activedescendant', li.id); li.scrollIntoView({block: 'nearest'}); }
	});
}

function choose(i) {
	var e = shownEntries[i], t = tail();
	if (!e || !t) return;
	var put = e.key + ':"' + e.val.replace(/"/g, '') + '" ';
	q.value = q.value.slice(0, t.start) + put + q.value.slice(t.end).replace(/^\s+/, '');
	q.setSelectionRange(t.start + put.length, t.start + put.length);
	open(false);
	apply();
}

var timer;
q.addEventListener('input', function () {
	clearTimeout(timer);
	timer = setTimeout(function () { apply(); suggest(); }, 100);
});
q.addEventListener('keydown', function (ev) {
	if (ev.key === 'ArrowDown' && list.hidden) { suggest(); if (shownEntries.length) move(1); ev.preventDefault(); }
	else if (list.hidden) return;
	else if (ev.key === 'ArrowDown') { move(1); ev.preventDefault(); }
	else if (ev.key === 'ArrowUp') { move(-1); ev.preventDefault(); }
	else if (ev.key === 'Enter' && active >= 0) { choose(active); ev.preventDefault(); }
	else if (ev.key === 'Tab' && active >= 0) { choose(active); ev.preventDefault(); }
	else if (ev.key === 'Escape') { open(false); ev.preventDefault(); }
});
q.addEventListener('blur', function () { clearTimeout(timer); apply(); open(false); });
age.addEventListener('change', apply);
root.querySelector('.filter').addEventListener('submit', function (ev) {
	ev.preventDefault();
	clearTimeout(timer);
	open(false);
	apply();
});
var params = new URLSearchParams(location.search);
q.value = params.get('q') || '';
age.value = params.get('age') || '';
if (age.selectedIndex < 0) age.value = '';
if (q.value || age.value) apply();

/* ---- sorting ---- */

sections.forEach(function (s) {
	var ths = [].slice.call(s.el.querySelectorAll('th'));
	ths.forEach(function (th, col) {
		th.addEventListener('click', function () {
			var was = th.getAttribute('aria-sort');
			var dir = !was ? 'ascending' : was === 'ascending' ? 'descending' : '';
			ths.forEach(function (x) { x.removeAttribute('aria-sort'); });
			var order = s.rows.slice(), sign = dir === 'ascending' ? 1 : -1;
			function text(r) { return r.el.cells[col].textContent.replace(/^[BP] (?=\d)/, ''); }
			if (dir) {
				th.setAttribute('aria-sort', dir);
				order.sort(function (a, b) {
					return sign * text(a).localeCompare(text(b), undefined, {numeric: true}) || a.i - b.i;
				});
			}
			var body = s.el.querySelector('tbody');
			order.forEach(function (r) { body.appendChild(r.el); });
		});
	});
});
})();
