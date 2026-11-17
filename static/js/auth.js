// Use the identity supplied by Django's protected dashboard view as a fallback
// when browser localStorage is unavailable or has been cleared.
try {
  const serverUserNode = document.getElementById('agri-server-user');
  if (serverUserNode) window.AGRI_SERVER_USER = JSON.parse(serverUserNode.textContent);
} catch (_) {}

/**
 * AgriConnect data layer.
 * MongoDB is the permanent store; this file keeps only a small UI session/cache.
 */
const Auth = (() => {
  const KEYS = { USERS: 'agri_users', SESSION: 'agri_session', PRODUCTS: 'agri_products', ORDERS: 'agri_orders' };

  function read(key) {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return null; }
  }
  function write(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; }
    catch (_) { return false; }
  }
  function csrfToken() {
    const field = document.querySelector('[name=csrfmiddlewaretoken]');
    if (field && field.value) return field.value;
    const cookie = document.cookie.split('; ').find(x => x.startsWith('csrftoken='));
    return cookie ? decodeURIComponent(cookie.split('=').slice(1).join('=')) : '';
  }
  async function request(url, method = 'GET', data) {
    const response = await fetch(url, {
      method, credentials: 'same-origin',
      headers: {
        ...(data !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(method !== 'GET' ? { 'X-CSRFToken': csrfToken() } : {})
      },
      ...(data !== undefined ? { body: JSON.stringify(data) } : {})
    });
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('The server returned an invalid response.'); }
    if (!response.ok || result.success === false) throw new Error(result.message || `Request failed (${response.status}).`);
    return result;
  }
  function getSession() {
    const cached = read(KEYS.SESSION);
    const serverUser = window.AGRI_SERVER_USER;
    if (serverUser) {
      const sameUser = cached?.user && String(cached.user.id) === String(serverUser.id);
      const user = sameUser ? { ...cached.user, ...serverUser } : serverUser;
      return { userId: serverUser.id, role: serverUser.role, user };
    }
    return cached;
  }
  function getCurrentUser() { return getSession()?.user || null; }
  function setSession(user) {
    // Django's signed server session is authoritative; localStorage is only a UI cache.
    window.AGRI_SERVER_USER = user;
    write(KEYS.SESSION, { userId: user.id, role: user.role, user });
    const users = getUsers().filter(u => u.id !== user.id);
    users.push(user); write(KEYS.USERS, users);
  }
  function getUsers() { return read(KEYS.USERS) || []; }
  function getUsersByRole(role) { return getUsers().filter(u => u.role === role); }
  function getUserById(id) { return getUsers().find(u => String(u.id) === String(id)) || null; }
  function getUserByUsername(username) {
    return getUsers().find(u => (u.username || '').toLowerCase() === (username || '').toLowerCase()) || null;
  }
  async function refresh() {
    if (!getCurrentUser()) return;
    const [p, o] = await Promise.all([request('/api/products/'), request('/api/orders/')]);
    write(KEYS.PRODUCTS, p.products || []);
    write(KEYS.ORDERS, o.orders || []);
    if (getCurrentUser()?.role === 'admin') {
      const u = await request('/api/users/');
      write(KEYS.USERS, u.users || []);
    } else {
      const me = getCurrentUser();
      write(KEYS.USERS, getUsers().filter(u => u.id !== me.id).concat([me]));
    }
  }
  async function login(username, password, role) {
    try {
      const result = await request('/api/login/', 'POST', { username, password, role });
      setSession(result.user);
      await refresh().catch(() => {});
      return { ok: true, user: result.user };
    } catch (error) { return { ok: false, error: error.message }; }
  }
  async function logout() {
    try { await request('/api/logout/', 'POST'); }
    finally {
      Object.values(KEYS).forEach(key => localStorage.removeItem(key));
    }
  }
  function requireRole(role, redirectUrl = '/login/') {
    const user = getCurrentUser();
    // The Django view has already enforced the role before rendering this page.
    // Use its server-provided identity if browser storage is unavailable or empty.
    if (!user || user.role !== role) {
      if (window.AGRI_SERVER_USER && window.AGRI_SERVER_USER.role === role) return window.AGRI_SERVER_USER;
      window.location.replace(redirectUrl);
      return null;
    }
    return user;
  }
  function redirectIfLoggedIn() {
    const user = getCurrentUser();
    if (!user) return;
    const destinations = { admin: '/admin-dashboard/', farmer: '/farmer-dashboard/', buyer: '/buyer-dashboard/' };
    window.location.href = destinations[user.role] || '/';
  }
  async function getSecurityQuestion(username) {
    try { const r = await request('/api/password-reset/', 'POST', { action: 'question', username }); return { ok: true, question: r.question }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  async function verifySecurityAnswer(username, answer) {
    try { await request('/api/password-reset/', 'POST', { action: 'verify', username, answer }); return { ok: true }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  async function resetPasswordWithAnswer(userId, password, confirmPassword) {
    if (!password || password.length < 6) return { ok: false, error: 'Password must contain at least 6 characters.' };
    if (password !== confirmPassword) return { ok: false, error: 'Passwords do not match.' };
    try { await request('/api/password-reset/', 'POST', { action: 'reset', password }); return { ok: true }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  async function updateUser(id, changes) {
    try {
      const result = await request(`/api/users/${encodeURIComponent(id)}/`, 'PATCH', changes);
      const updated = result.user;
      write(KEYS.USERS, getUsers().filter(u => u.id !== id).concat([updated]));
      if (getCurrentUser()?.id === id) setSession(updated);
      return { ok: true, user: updated };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  async function deleteUser(id) {
    try { await request(`/api/users/${encodeURIComponent(id)}/`, 'DELETE'); write(KEYS.USERS, getUsers().filter(u => u.id !== id)); return { ok: true }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  function getProducts() { return read(KEYS.PRODUCTS) || []; }
  function getProductsByFarmer(id) { return getProducts().filter(p => String(p.farmerId) === String(id)); }
  function getApprovedProducts() { return getProducts().filter(p => (p.status || 'Approved').toLowerCase() === 'approved'); }
  async function addProduct(product) {
    try {
      const r = await request('/api/products/', 'POST', product);
      await refresh();
      return { ok: true, product: r.product };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  async function updateProduct(id, changes) {
    try { const r = await request(`/api/products/${encodeURIComponent(id)}/`, 'PATCH', changes); await refresh(); return { ok: true, product: r.product }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  async function deleteProduct(id) {
    try { await request(`/api/products/${encodeURIComponent(id)}/`, 'DELETE'); await refresh(); return { ok: true }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  function getOrders() { return read(KEYS.ORDERS) || []; }
  async function addOrder(order) {
    try {
      const r = await request('/api/orders/', 'POST', { productId: order.productId, quantity: order.quantity });
      await refresh();
      return { ok: true, order: r.order };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  async function updateOrder(id, changes) {
    try { await request(`/api/orders/${encodeURIComponent(id)}/`, 'PATCH', changes); await refresh(); return { ok: true }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  const ready = refresh().catch(error => console.warn('AgriConnect data could not be loaded:', error.message));
  return {
    ready, refresh, login, logout, setSession, getSession, getCurrentUser, requireRole, redirectIfLoggedIn,
    getUsers, getUsersByRole, getUserById, getUserByUsername, updateUser, deleteUser,
    getSecurityQuestion, verifySecurityAnswer, resetPasswordWithAnswer,
    getProducts, getProductsByFarmer, getApprovedProducts, addProduct, updateProduct, deleteProduct,
    getOrders, addOrder, updateOrder
  };
})();
