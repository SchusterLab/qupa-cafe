<?php
/**
 * Tells the Qupa Cafe forum that a page of this wiki changed, so that the
 * forum's copy of the page follows within seconds. Load it from
 * LocalSettings.php, after setting the two values it reads:
 *
 *     $wgQupaCafeNotifyUrl = 'https://forum.example.com/host/wiki-changed';
 *     $wgQupaCafeNotifySecret = '<the secret in the mirror's WIKI_HOOK_SECRET>';
 *     require_once __DIR__ . '/wiki-notify.php';
 *
 * A notice names no page and carries nothing from the wiki. It is the kind of
 * change and the time, signed with the secret, and the mirror then asks the
 * wiki what changed. It is sent after the editor's page has been answered, so
 * an edit never waits for the forum. A notice that is lost costs only time:
 * the mirror also looks every fifteen minutes. docs/05-forum-config.md 5.11.
 */

use MediaWiki\Deferred\DeferredUpdates;
use MediaWiki\MediaWikiServices;

function qupaCafeNotify( string $event ): void {
	global $wgQupaCafeNotifyUrl, $wgQupaCafeNotifySecret;
	if ( !$wgQupaCafeNotifyUrl || !$wgQupaCafeNotifySecret ) {
		return;
	}
	$url = $wgQupaCafeNotifyUrl;
	$secret = $wgQupaCafeNotifySecret;
	DeferredUpdates::addCallableUpdate( static function () use ( $event, $url, $secret ) {
		$body = json_encode( [ 'event' => $event, 'at' => time() ] );
		$request = MediaWikiServices::getInstance()->getHttpRequestFactory()->create(
			$url,
			[ 'method' => 'POST', 'postData' => $body, 'timeout' => 5, 'connectTimeout' => 3 ],
			__FUNCTION__
		);
		$request->setHeader( 'Content-Type', 'application/json' );
		$request->setHeader( 'X-Qupa-Signature', 'sha256=' . hash_hmac( 'sha256', $body, $secret ) );
		$status = $request->execute();
		if ( !$status->isOK() ) {
			wfDebugLog( 'qupacafe', "The forum did not take the notice: HTTP {$request->getStatus()}" );
		}
	} );
}

$wgHooks['PageSaveComplete'][] = static function () {
	qupaCafeNotify( 'edit' );
};
$wgHooks['PageMoveComplete'][] = static function () {
	qupaCafeNotify( 'move' );
};
$wgHooks['PageDeleteComplete'][] = static function () {
	qupaCafeNotify( 'deletion' );
};
$wgHooks['PageUndeleteComplete'][] = static function () {
	qupaCafeNotify( 'restoration' );
};
