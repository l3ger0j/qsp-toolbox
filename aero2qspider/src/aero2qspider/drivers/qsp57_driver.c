/*
 * Step driver for libqsp 5.7 used by `aero2qspider convert --compare`.
 *
 * Usage: qsp57-driver GAME.qsp
 * Reads one command per line from stdin and prints the resulting state as one JSON line:
 *   exec <hex utf-8 code>   run code
 *   act <index>             select and execute an action
 *   obj <index>             select an object
 *   tick                    run the counter location
 *   quiet <hex utf-8 code>  run code without printing the state
 *   quit
 * The protocol and the state format are the same as in qsp59-driver.mjs.
 */
#include <locale.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "bindings/default/qsp_default.h"

typedef struct {
	char *data;
	size_t len, cap;
} Buf;

static void buf_add(Buf *b, const char *s, size_t n)
{
	if (b->len + n + 1 > b->cap) {
		b->cap = (b->len + n + 1) * 2;
		b->data = realloc(b->data, b->cap);
	}
	memcpy(b->data + b->len, s, n);
	b->len += n;
	b->data[b->len] = 0;
}

static void buf_str(Buf *b, const char *s) { buf_add(b, s, strlen(s)); }

static void buf_utf8(Buf *b, unsigned long c)
{
	char out[4];
	size_t n;
	if (c < 0x80) { out[0] = (char)c; n = 1; }
	else if (c < 0x800) { out[0] = (char)(0xC0 | (c >> 6)); out[1] = (char)(0x80 | (c & 0x3F)); n = 2; }
	else if (c < 0x10000) {
		out[0] = (char)(0xE0 | (c >> 12)); out[1] = (char)(0x80 | ((c >> 6) & 0x3F)); out[2] = (char)(0x80 | (c & 0x3F)); n = 3;
	} else {
		out[0] = (char)(0xF0 | (c >> 18)); out[1] = (char)(0x80 | ((c >> 12) & 0x3F));
		out[2] = (char)(0x80 | ((c >> 6) & 0x3F)); out[3] = (char)(0x80 | (c & 0x3F)); n = 4;
	}
	buf_add(b, out, n);
}

/* Append a wide string as a JSON string literal (wchar_t is UTF-16 on Windows and UTF-32 elsewhere). */
static void buf_json(Buf *b, const wchar_t *s)
{
	char esc[8];
	buf_str(b, "\"");
	for (; s && *s; ++s) {
		unsigned long c = (unsigned long)*s;
		if (sizeof(wchar_t) == 2 && c >= 0xD800 && c < 0xDC00 && s[1] >= 0xDC00 && s[1] < 0xE000) {
			c = 0x10000 + ((c - 0xD800) << 10) + ((unsigned long)s[1] - 0xDC00);
			++s;
		}
		if (c == '"' || c == '\\') { buf_str(b, "\\"); buf_utf8(b, c); }
		else if (c < 0x20) { sprintf(esc, "\\u%04lx", c); buf_str(b, esc); }
		else buf_utf8(b, c);
	}
	buf_str(b, "\"");
}

static wchar_t *from_utf8(const unsigned char *s, size_t n)
{
	wchar_t *out = malloc((n + 1) * sizeof(wchar_t) * 2);
	size_t i = 0, o = 0;
	while (i < n) {
		unsigned long c = s[i];
		int extra = c >= 0xF0 ? 3 : c >= 0xE0 ? 2 : c >= 0xC0 ? 1 : 0;
		if (extra) c &= 0x3F >> extra;
		++i;
		while (extra-- > 0 && i < n) c = (c << 6) | (s[i++] & 0x3F);
		if (sizeof(wchar_t) == 2 && c >= 0x10000) {
			c -= 0x10000;
			out[o++] = (wchar_t)(0xD800 + (c >> 10));
			out[o++] = (wchar_t)(0xDC00 + (c & 0x3FF));
		} else {
			out[o++] = (wchar_t)c;
		}
	}
	out[o] = 0;
	return out;
}

static wchar_t *from_hex(const char *hex)
{
	size_t n = strlen(hex) / 2, i;
	unsigned char *bytes = malloc(n + 1);
	wchar_t *result;
	for (i = 0; i < n; ++i) {
		unsigned int v;
		sscanf(hex + 2 * i, "%2x", &v);
		bytes[i] = (unsigned char)v;
	}
	result = from_utf8(bytes, n);
	free(bytes);
	return result;
}

static Buf events;
#define MAX_DIALOGS 200

static int answers = 0;
static int menu_items = 0;
static int dialogs = 0;

/* answers to INPUT; the same list is used by qsp59-driver.mjs */
static const wchar_t *inputs[] = {L"1", L"0", L"2", L"", L"3"};

static void dialog(void)
{
	if (++dialogs > MAX_DIALOGS) {
		fprintf(stderr, "the game opened more than %d dialogs in one step (an endless loop?)\n", MAX_DIALOGS);
		exit(3);
	}
}

static void event(const char *kind, const wchar_t *text)
{
	buf_str(&events, events.len ? ",[\"" : "[\"");
	buf_str(&events, kind);
	buf_str(&events, "\",");
	buf_json(&events, text);
	buf_str(&events, "]");
}

static void cb_noop(void) {}
static int cb_zero(void) { return 0; }
static void cb_msg(const wchar_t *text) { dialog(); event("msg", text); }
/* libqsp 5.7 prepends the game folder to file names; QSP 5.9 passes them as written */
static const wchar_t *game_dir = L"";
static size_t game_dir_len = 0;

static const wchar_t *relative(const wchar_t *path)
{
	return wcsncmp(path, game_dir, game_dir_len) ? path : path + game_dir_len;
}

static void cb_view(const wchar_t *path) { if (path && *path) event("view", relative(path)); }
static void cb_play(const wchar_t *path, int volume) { (void)volume; event("play", relative(path)); }
static void cb_system(const wchar_t *cmd) { event("system", cmd); }
static void cb_delete_menu(void) { menu_items = 0; }
static void cb_add_menu_item(const wchar_t *name, const wchar_t *image) { (void)image; event("menu", name); ++menu_items; }
static int cb_show_menu(void) { dialog(); return menu_items ? answers++ % menu_items : -1; }

static void cb_input(const wchar_t *text, wchar_t *buffer, int max_len)
{
	(void)text;
	dialog();
	wcsncpy(buffer, inputs[answers++ % 5], max_len - 1);
	buffer[max_len - 1] = 0;
}

static void print_list(Buf *out, int count, void (*get)(int, QSP_CHAR **, QSP_CHAR **))
{
	int i;
	buf_str(out, "[");
	for (i = 0; i < count; ++i) {
		QSP_CHAR *image, *desc;
		get(i, &image, &desc);
		if (i) buf_str(out, ",");
		buf_json(out, desc);
	}
	buf_str(out, "]");
}

static void print_state(QSP_BOOL ok)
{
	Buf out = {0};
	char num[64];
	buf_str(&out, "{\"main\":");
	buf_json(&out, QSPGetMainDesc());
	buf_str(&out, ",\"stats\":");
	buf_json(&out, QSPGetVarsDesc());
	buf_str(&out, ",\"actions\":");
	print_list(&out, QSPGetActionsCount(), QSPGetActionData);
	buf_str(&out, ",\"objects\":");
	print_list(&out, QSPGetObjectsCount(), QSPGetObjectData);
	buf_str(&out, ",\"location\":");
	buf_json(&out, QSPGetCurLoc());
	buf_str(&out, ",\"events\":[");
	if (events.len) buf_add(&out, events.data, events.len);
	buf_str(&out, "],\"error\":");
	if (ok) {
		buf_str(&out, "null");
	} else {
		int code, act, line;
		QSP_CHAR *loc;
		QSPGetLastErrorData(&code, &loc, &act, &line);
		buf_str(&out, "{\"location\":");
		buf_json(&out, loc);
		sprintf(num, ",\"action\":%d,\"line\":%d,\"description\":", act, line);
		buf_str(&out, num);
		buf_json(&out, QSPGetErrorDesc(code));
		buf_str(&out, "}");
	}
	buf_str(&out, "}\n");
	fwrite(out.data, 1, out.len, stdout);
	fflush(stdout);
	free(out.data);
	events.len = 0;
	if (events.data) events.data[0] = 0;
}

int main(int argc, char **argv)
{
	static char line[1 << 20];
	wchar_t *path;
	if (argc < 2) {
		fprintf(stderr, "usage: %s GAME.qsp\n", argv[0]);
		return 2;
	}
	/* libqsp converts file names with wcstombs */
	if (!setlocale(LC_CTYPE, "C.UTF-8") && !setlocale(LC_CTYPE, "en_US.UTF-8") && !setlocale(LC_CTYPE, ".UTF8"))
		setlocale(LC_CTYPE, "");
	QSPInit();
	QSPSetCallBack(QSP_CALL_DEBUG, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_ISPLAYINGFILE, (QSP_CALLBACK)cb_zero);
	QSPSetCallBack(QSP_CALL_PLAYFILE, (QSP_CALLBACK)cb_play);
	QSPSetCallBack(QSP_CALL_CLOSEFILE, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_SHOWIMAGE, (QSP_CALLBACK)cb_view);
	QSPSetCallBack(QSP_CALL_SHOWWINDOW, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_DELETEMENU, (QSP_CALLBACK)cb_delete_menu);
	QSPSetCallBack(QSP_CALL_ADDMENUITEM, (QSP_CALLBACK)cb_add_menu_item);
	QSPSetCallBack(QSP_CALL_SHOWMENU, (QSP_CALLBACK)cb_show_menu);
	QSPSetCallBack(QSP_CALL_SHOWMSGSTR, (QSP_CALLBACK)cb_msg);
	QSPSetCallBack(QSP_CALL_REFRESHINT, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_SETTIMER, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_SETINPUTSTRTEXT, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_SYSTEM, (QSP_CALLBACK)cb_system);
	QSPSetCallBack(QSP_CALL_OPENGAMESTATUS, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_SAVEGAMESTATUS, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_SLEEP, (QSP_CALLBACK)cb_noop);
	QSPSetCallBack(QSP_CALL_GETMSCOUNT, (QSP_CALLBACK)cb_zero);
	QSPSetCallBack(QSP_CALL_INPUTBOX, (QSP_CALLBACK)cb_input);

	path = from_utf8((const unsigned char *)argv[1], strlen(argv[1]));
	for (size_t i = 0; path[i]; ++i)
		if (path[i] == L'/' || path[i] == L'\\') game_dir_len = i + 1;
	game_dir = path;
	if (!QSPLoadGameWorld(path)) {
		fprintf(stderr, "cannot load %s\n", argv[1]);
		return 1;
	}
	printf("{\"ready\":true}\n");
	fflush(stdout);

	while (fgets(line, sizeof(line), stdin)) {
		char *arg;
		QSP_BOOL ok = QSP_TRUE;
		line[strcspn(line, "\r\n")] = 0;
		dialogs = 0;
		arg = strchr(line, ' ');
		if (arg) *arg++ = 0; else arg = line + strlen(line);
		if (!strcmp(line, "quit")) {
			break;
		} else if (!strcmp(line, "exec") || !strcmp(line, "quiet")) {
			wchar_t *code = from_hex(arg);
			ok = QSPExecString(code, QSP_TRUE);
			free(code);
			if (!strcmp(line, "quiet")) {
				events.len = 0;
				continue;
			}
		} else if (!strcmp(line, "act")) {
			ok = QSPSetSelActionIndex(atoi(arg), QSP_FALSE) && QSPExecuteSelActionCode(QSP_TRUE);
		} else if (!strcmp(line, "obj")) {
			ok = QSPSetSelObjectIndex(atoi(arg), QSP_TRUE);
		} else if (!strcmp(line, "tick")) {
			ok = QSPExecCounter(QSP_TRUE);
		} else {
			fprintf(stderr, "unknown command: %s\n", line);
			continue;
		}
		print_state(ok);
	}
	QSPDeInit();
	return 0;
}
