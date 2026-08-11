/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";

const LOCAL_DEV_HOSTS = new Set(["localhost", "127.0.0.1"]);

export const getBrowserAlignedApiBaseUrl = () => {
  if (typeof window === "undefined" || !API_BASE_URL) return API_BASE_URL;

  try {
    const apiUrl = new URL(API_BASE_URL);
    const pageHostname = window.location.hostname;

    if (LOCAL_DEV_HOSTS.has(apiUrl.hostname) && LOCAL_DEV_HOSTS.has(pageHostname)) {
      apiUrl.hostname = pageHostname;
    }

    return apiUrl.origin;
  } catch {
    return API_BASE_URL;
  }
};
